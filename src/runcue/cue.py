"""Core Cue orchestrator class."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from pathlib import Path
from typing import Any

import aiosqlite

from runcue import db
from runcue.models import TaskType, WorkState, WorkUnit

logger = logging.getLogger(__name__)


class Cue:
    """
    The main orchestrator for coordinating work across rate-limited services.
    
    Acts as an "air traffic controller" - dispatches work to external systems
    (AI APIs, SSH hosts, batch processors) and tracks completion.
    
    Args:
        db_path: Path to SQLite database file, or ":memory:" for in-memory DB.
        service_log_retention: Seconds to retain service log entries (default 24h).
    """
    
    def __init__(
        self,
        db_path: str | Path,
        *,
        service_log_retention: int = 86400,
    ) -> None:
        self.db_path = str(db_path)
        self.service_log_retention = service_log_retention
        self._conn: aiosqlite.Connection | None = None
        self._tasks: dict[str, TaskType] = {}
        self._initialized = False
        
        # Orchestrator state
        self._running = False
        self._loop_task: asyncio.Task | None = None
        self._stop_event: asyncio.Event | None = None
        self._active_work: set[str] = set()  # Currently executing work IDs
    
    async def _ensure_initialized(self) -> None:
        """Ensure database is initialized."""
        if not self._initialized:
            self._conn = await db.init_db(self.db_path)
            self._initialized = True
    
    @property
    def conn(self) -> aiosqlite.Connection:
        """Get database connection (must be initialized first)."""
        if self._conn is None:
            raise RuntimeError("Cue not initialized. Call await cue._ensure_initialized() first.")
        return self._conn
    
    async def close(self) -> None:
        """Close database connection."""
        if self._conn:
            await self._conn.close()
            self._conn = None
            self._initialized = False
    
    # --- Orchestrator Lifecycle ---
    
    def start(self) -> None:
        """
        Start the orchestrator loop.
        
        Non-blocking - starts the scheduling loop as a background asyncio task.
        Call await stop() for graceful shutdown.
        """
        if self._running:
            return
        
        self._running = True
        self._stop_event = asyncio.Event()
        self._loop_task = asyncio.create_task(self._run_loop())
        logger.info("Orchestrator started")
    
    async def stop(self) -> None:
        """
        Stop the orchestrator gracefully.
        
        Waits for currently running work to complete before returning.
        Does NOT close the database - call close() separately when done.
        """
        if not self._running:
            return
        
        logger.info("Stopping orchestrator...")
        self._running = False
        
        if self._stop_event:
            self._stop_event.set()
        
        # Wait for active work to complete
        if self._active_work:
            logger.info(f"Waiting for {len(self._active_work)} active work units...")
            while self._active_work:
                await asyncio.sleep(0.01)
        
        # Wait for loop task to finish
        if self._loop_task:
            try:
                await asyncio.wait_for(self._loop_task, timeout=5.0)
            except asyncio.TimeoutError:
                logger.warning("Loop task did not finish in time, cancelling")
                self._loop_task.cancel()
                try:
                    await self._loop_task
                except asyncio.CancelledError:
                    pass
            self._loop_task = None
        
        logger.info("Orchestrator stopped")
    
    async def _run_loop(self) -> None:
        """Main orchestrator loop - picks and dispatches work."""
        await self._ensure_initialized()
        await self._persist_services()
        
        while self._running:
            try:
                # Try to dispatch work
                dispatched = await self._dispatch_next()
                
                if not dispatched:
                    # No work to do, wait a bit but check for stop signal
                    try:
                        await asyncio.wait_for(
                            self._stop_event.wait() if self._stop_event else asyncio.sleep(0.05),
                            timeout=0.01,
                        )
                    except asyncio.TimeoutError:
                        pass
                else:
                    # Yield to allow other tasks to run
                    await asyncio.sleep(0)
            except Exception as e:
                logger.exception(f"Error in orchestrator loop: {e}")
                await asyncio.sleep(0.01)
    
    async def _dispatch_next(self) -> bool:
        """
        Find and dispatch the next eligible work unit.
        
        Returns True if work was dispatched, False if queue is empty.
        """
        # Find queued work (for now, simple FIFO - no rate limiting yet)
        async with self.conn.execute(
            """
            SELECT * FROM work_units 
            WHERE state = ? 
            ORDER BY created_at ASC 
            LIMIT 1
            """,
            (WorkState.QUEUED.value,),
        ) as cursor:
            row = await cursor.fetchone()
        
        if not row:
            return False
        
        work = WorkUnit.from_row(dict(row))
        
        # Mark as running
        work.state = WorkState.RUNNING
        work.started_at = time.time()
        await db.save_work_unit(self.conn, work.to_row())
        
        # Execute in background
        self._active_work.add(work.id)
        asyncio.create_task(self._execute_work(work))
        
        return True
    
    async def _execute_work(self, work: WorkUnit) -> None:
        """Execute a work unit and update its state."""
        try:
            # Get the task type
            task = self._tasks.get(work.task_type)
            if not task:
                raise ValueError(f"Unknown task type: {work.task_type}")
            
            if not task.handler:
                raise ValueError(f"No handler for task type: {work.task_type}")
            
            logger.debug(f"Executing work {work.id} (task={work.task_type})")
            
            # Call the handler
            if asyncio.iscoroutinefunction(task.handler):
                result = await task.handler(work)
            else:
                result = task.handler(work)
            
            # Success - update work unit
            work.state = WorkState.COMPLETED
            work.completed_at = time.time()
            work.result = result if isinstance(result, dict) else {"result": result}
            
            logger.debug(f"Work {work.id} completed successfully")
            
        except Exception as e:
            # Failure - update work unit
            work.state = WorkState.FAILED
            work.completed_at = time.time()
            work.error = str(e)
            
            logger.warning(f"Work {work.id} failed: {e}")
        
        finally:
            # Persist final state
            await db.save_work_unit(self.conn, work.to_row())
            self._active_work.discard(work.id)
    
    # --- Service Registration ---
    
    def service(
        self,
        name: str,
        *,
        rate_limit: tuple[int, int] | None = None,
        max_concurrent: int | None = None,
        circuit_threshold: int = 5,
        circuit_timeout: int = 300,
    ) -> None:
        """
        Register a rate-limited service.
        
        Services are stored in memory until the orchestrator is started,
        then persisted to the database.
        
        Args:
            name: Unique service identifier.
            rate_limit: Tuple of (requests, window_seconds), e.g., (60, 60) for 60/min.
            max_concurrent: Maximum simultaneous requests allowed.
            circuit_threshold: Failures before circuit opens.
            circuit_timeout: Seconds before circuit half-opens.
        """
        # Store service config - will be persisted when started
        service_data = {
            "name": name,
            "max_concurrent": max_concurrent,
            "rate_limit": rate_limit[0] if rate_limit else None,
            "rate_window": rate_limit[1] if rate_limit else None,
            "circuit_threshold": circuit_threshold,
            "circuit_timeout": circuit_timeout,
        }
        # Store in a temporary dict for sync registration
        if not hasattr(self, "_pending_services"):
            self._pending_services: dict[str, dict] = {}
        self._pending_services[name] = service_data
    
    async def _persist_services(self) -> None:
        """Persist all pending services to database."""
        if hasattr(self, "_pending_services"):
            for service_data in self._pending_services.values():
                await db.save_service(self.conn, service_data)
    
    async def get_service(self, name: str) -> dict | None:
        """Get a service by name from the database."""
        await self._ensure_initialized()
        return await db.get_service(self.conn, name)
    
    async def list_services(self) -> list[dict]:
        """List all registered services."""
        await self._ensure_initialized()
        return await db.get_all_services(self.conn)
    
    # --- Task Registration ---
    
    def task(
        self,
        name: str,
        *,
        services: list[str] | None = None,
        executor: str = "async",
        retry: int | dict[str, Any] | None = None,
    ):
        """
        Decorator to register a task type.
        
        Args:
            name: Unique task identifier.
            services: List of service names this task requires.
            executor: How to run: "async", "process_pool", or "subprocess".
            retry: Retry count or policy dict.
        """
        def decorator(func):
            # Parse retry config
            if isinstance(retry, int):
                max_attempts = retry
                backoff = "exponential"
                base_delay = 1.0
                max_delay = 300.0
            elif isinstance(retry, dict):
                max_attempts = retry.get("max_attempts", 1)
                backoff = retry.get("backoff", "exponential")
                base_delay = retry.get("base_delay", 1.0)
                max_delay = retry.get("max_delay", 300.0)
            else:
                max_attempts = 1
                backoff = "exponential"
                base_delay = 1.0
                max_delay = 300.0
            
            self._tasks[name] = TaskType(
                name=name,
                services=services or [],
                executor=executor,
                handler=func,
                retry_max_attempts=max_attempts,
                retry_backoff=backoff,
                retry_base_delay=base_delay,
                retry_max_delay=max_delay,
            )
            return func
        return decorator
    
    def get_task(self, name: str) -> TaskType | None:
        """Get a registered task type."""
        return self._tasks.get(name)
    
    # --- Work Submission ---
    
    async def submit(
        self,
        task_type: str,
        *,
        target: str | None = None,
        params: dict[str, Any] | None = None,
        depends_on: list[str] | None = None,
        timeout: float = 300.0,
        dependency_timeout: float | None = None,
        idempotency_key: str | None = None,
    ) -> str:
        """
        Submit a work unit to the queue.
        
        Args:
            task_type: Name of registered task type.
            target: What to process (file path, record ID, URL, etc.).
            params: Task-specific parameters.
            depends_on: IDs of work units that must complete first.
            timeout: Maximum execution time in seconds.
            dependency_timeout: How long to wait for dependencies.
            idempotency_key: Optional key for duplicate detection.
            
        Returns:
            Work unit ID.
            
        Raises:
            ValueError: If task type not registered.
        """
        await self._ensure_initialized()
        await self._persist_services()
        
        # Validate task type
        task = self._tasks.get(task_type)
        if not task:
            raise ValueError(f"Unknown task type: {task_type}")
        
        # Check idempotency
        if idempotency_key:
            existing = await db.get_work_by_idempotency_key(self.conn, idempotency_key)
            if existing:
                return existing["id"]
        
        # Create work unit
        work_id = str(uuid.uuid4())
        work = WorkUnit(
            id=work_id,
            task_type=task_type,
            target=target,
            params=params or {},
            state=WorkState.QUEUED,
            created_at=time.time(),
            attempt=1,
            max_attempts=task.retry_max_attempts,
            depends_on=depends_on or [],
            timeout=timeout,
            dependency_timeout=dependency_timeout,
            idempotency_key=idempotency_key,
        )
        
        # Persist
        await db.save_work_unit(self.conn, work.to_row())
        
        return work_id
    
    async def get(self, work_id: str) -> WorkUnit | None:
        """Get a work unit by ID."""
        await self._ensure_initialized()
        row = await db.get_work_unit(self.conn, work_id)
        return WorkUnit.from_row(row) if row else None
    
    async def list(
        self,
        *,
        state: str | WorkState | None = None,
        task_type: str | None = None,
        limit: int = 100,
    ) -> list[WorkUnit]:
        """
        List work units with optional filters.
        
        Args:
            state: Filter by state (e.g., "queued", "running").
            task_type: Filter by task type.
            limit: Maximum results to return.
        """
        await self._ensure_initialized()
        
        state_str = state.value if isinstance(state, WorkState) else state
        rows = await db.list_work_units(
            self.conn,
            state=state_str,
            task_type=task_type,
            limit=limit,
        )
        return [WorkUnit.from_row(row) for row in rows]
    
    # --- For Phase 0 compatibility (sync access to internal state) ---
    
    @property
    def _services(self) -> dict[str, dict]:
        """Access pending services (for tests)."""
        if not hasattr(self, "_pending_services"):
            self._pending_services = {}
        return self._pending_services
