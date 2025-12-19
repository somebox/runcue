"""Core Cue orchestrator class."""

from __future__ import annotations

import asyncio
import inspect
import time
import uuid
from typing import Any, Callable

from runcue.models import PriorityContext, TaskType, WorkState, WorkUnit


class Cue:
    """
    Control tower for coordinating work across rate-limited services.
    
    runcue decides WHEN work runs. You decide WHAT it does.
    
    Fully in-memory. No persistence. Artifacts are the source of truth.
    
    Example:
        cue = runcue.Cue()
        cue.service("openai", rate="60/min", concurrent=5)
        
        @cue.task("extract", uses="openai")
        def extract(work):
            return {"text": "..."}
        
        @cue.is_ready
        def is_ready(work):
            return Path(work.params["input"]).exists()
        
        cue.start()
        await cue.submit("extract", params={"input": "doc.pdf"})
        await cue.stop()
    """
    
    def __init__(self) -> None:
        # Task and service definitions
        self._tasks: dict[str, TaskType] = {}
        self._services: dict[str, dict] = {}
        
        # Callbacks
        self._is_ready_callback: Callable | None = None
        self._is_stale_callback: Callable | None = None
        self._priority_callback: Callable | None = None
        self._on_complete_callback: Callable | None = None
        self._on_failure_callback: Callable | None = None
        self._on_skip_callback: Callable | None = None
        self._on_start_callback: Callable | None = None
        
        # In-memory work storage
        self._queue: list[WorkUnit] = []              # Pending work
        self._active: dict[str, WorkUnit] = {}        # Running work
        self._completed: dict[str, WorkUnit] = {}     # Completed/failed/cancelled
        
        # Rate limit tracking (to be implemented in Phase 3)
        self._service_active: dict[str, set[str]] = {}
        self._service_requests: dict[str, list[float]] = {}
        
        # Orchestrator state
        self._running = False
        self._orchestrator_task: asyncio.Task | None = None
        self._work_tasks: dict[str, asyncio.Task] = {}  # work_id -> task
    
    # --- Service Registration ---
    
    def service(
        self,
        name: str,
        *,
        rate: str | None = None,
        concurrent: int | None = None,
    ) -> None:
        """
        Register a rate-limited service.
        
        Args:
            name: Unique service identifier.
            rate: Rate limit string, e.g., "60/min", "1000/hour", "10/sec".
            concurrent: Maximum simultaneous requests.
        
        Example:
            cue.service("openai", rate="60/min", concurrent=5)
            cue.service("local", concurrent=4)  # No rate limit
        """
        rate_limit = None
        rate_window = None
        
        if rate:
            rate_limit, rate_window = self._parse_rate(rate)
        
        self._services[name] = {
            "name": name,
            "rate_limit": rate_limit,
            "rate_window": rate_window,
            "concurrent": concurrent,
        }
        
        # Initialize tracking structures
        self._service_active[name] = set()
        self._service_requests[name] = []
    
    def _parse_rate(self, rate: str) -> tuple[int, int]:
        """Parse rate string like '60/min' into (count, seconds)."""
        parts = rate.split("/")
        if len(parts) != 2:
            raise ValueError(f"Invalid rate format: {rate}. Use 'N/min', 'N/hour', 'N/sec'.")
        
        count = int(parts[0])
        unit = parts[1].lower()
        
        if unit in ("s", "sec", "second"):
            window = 1
        elif unit in ("m", "min", "minute"):
            window = 60
        elif unit in ("h", "hr", "hour"):
            window = 3600
        else:
            raise ValueError(f"Unknown rate unit: {unit}. Use 'sec', 'min', or 'hour'.")
        
        return count, window
    
    # --- Task Registration ---
    
    def task(
        self,
        name: str,
        *,
        uses: str | None = None,
        retry: int = 1,
    ):
        """
        Decorator to register a task type.
        
        Args:
            name: Unique task identifier.
            uses: Service name this task requires.
            retry: Maximum attempts (reserved for future use).
        
        Example:
            @cue.task("extract", uses="openai")
            def extract(work):
                return {"text": call_api(work.params["input"])}
        """
        # Validate service exists if specified
        if uses is not None and uses not in self._services:
            raise ValueError(f"Unknown service: {uses}")
        
        def decorator(func):
            self._tasks[name] = TaskType(
                name=name,
                service=uses,
                handler=func,
                retry=retry,
            )
            return func
        return decorator
    
    def get_task(self, name: str) -> TaskType | None:
        """Get a registered task type."""
        return self._tasks.get(name)
    
    # --- Artifact Check Callbacks ---
    
    def is_ready(self, func):
        """
        Decorator to register the readiness callback.
        
        Called before scheduling to check if inputs are valid.
        Return True if work can run, False to stay pending.
        
        Example:
            @cue.is_ready
            def is_ready(work) -> bool:
                if work.task == "extract":
                    return Path(work.params["input"]).exists()
                return True
        """
        self._is_ready_callback = func
        return func
    
    def is_stale(self, func):
        """
        Decorator to register the staleness callback.
        
        Called after is_ready passes. Return True if work should run,
        False to skip (output already valid).
        
        Example:
            @cue.is_stale
            def is_stale(work) -> bool:
                if work.task == "extract":
                    output = work.params["input"].replace(".pdf", ".txt")
                    return not Path(output).exists()
                return True  # Default: always run
        """
        self._is_stale_callback = func
        return func
    
    def priority(self, func):
        """
        Decorator to register the priority callback.
        
        Returns 0.0 (lowest) to 1.0 (highest). Higher priority runs first.
        
        Example:
            @cue.priority
            def prioritize(ctx) -> float:
                if ctx.work.params.get("urgent"):
                    return 1.0
                return 0.5
        """
        self._priority_callback = func
        return func
    
    # --- Event Callbacks ---
    
    def on_complete(self, func):
        """
        Decorator to register completion callback.
        
        Called after successful completion with (work, result, duration).
        
        Example:
            @cue.on_complete
            def on_complete(work, result, duration):
                logging.info(f"{work.task} completed in {duration:.2f}s")
        """
        self._on_complete_callback = func
        return func
    
    def on_failure(self, func):
        """
        Decorator to register failure callback.
        
        Called after failure with (work, error).
        
        Example:
            @cue.on_failure
            def on_failure(work, error):
                alerting.send(f"Failed: {work.task} - {error}")
        """
        self._on_failure_callback = func
        return func
    
    def on_skip(self, func):
        """
        Decorator to register skip callback.
        
        Called when work is skipped (is_stale returned False).
        
        Example:
            @cue.on_skip
            def on_skip(work):
                logging.debug(f"Skipped {work.task}")
        """
        self._on_skip_callback = func
        return func
    
    def on_start(self, func):
        """
        Decorator to register start callback.
        
        Called when work begins executing.
        
        Example:
            @cue.on_start
            def on_start(work):
                logging.info(f"Starting {work.task}")
        """
        self._on_start_callback = func
        return func
    
    # --- Lifecycle ---
    
    def start(self) -> None:
        """
        Start the orchestrator loop.
        
        Non-blocking - starts scheduling as a background asyncio task.
        """
        if self._running:
            return
        
        self._running = True
        # Get the current event loop and create the orchestrator task
        loop = asyncio.get_event_loop()
        self._orchestrator_task = loop.create_task(self._run_orchestrator())
    
    async def stop(self, timeout: float | None = None) -> None:
        """
        Stop the orchestrator gracefully.
        
        Args:
            timeout: Max seconds to wait for running work. None = wait forever.
        
        Waits for currently running work to complete, or until timeout.
        """
        self._running = False
        
        # Cancel the orchestrator loop
        if self._orchestrator_task is not None:
            self._orchestrator_task.cancel()
            try:
                await self._orchestrator_task
            except asyncio.CancelledError:
                pass
            self._orchestrator_task = None
        
        # Wait for active work to complete
        if self._work_tasks:
            tasks = list(self._work_tasks.values())
            if timeout is not None:
                # Wait with timeout
                done, pending = await asyncio.wait(
                    tasks,
                    timeout=timeout,
                    return_when=asyncio.ALL_COMPLETED
                )
                # Cancel any tasks that didn't complete
                for task in pending:
                    task.cancel()
                    try:
                        await task
                    except asyncio.CancelledError:
                        pass
            else:
                # Wait forever
                await asyncio.gather(*tasks, return_exceptions=True)
        
        self._work_tasks.clear()
    
    async def _run_orchestrator(self) -> None:
        """Background loop that dispatches pending work."""
        while self._running:
            # Collect work to dispatch (can't modify queue while iterating)
            to_dispatch = []
            remaining = []
            
            # Sort queue by priority (higher priority first)
            queue_depth = len(self._queue)
            sorted_queue = sorted(
                self._queue,
                key=lambda w: self._get_priority(w, queue_depth),
                reverse=True  # Higher priority first
            )
            
            for work in sorted_queue:
                task_type = self._tasks.get(work.task)
                if task_type is None:
                    remaining.append(work)
                    continue
                
                # Check if work is ready (inputs valid)
                if not self._check_is_ready(work):
                    remaining.append(work)
                    continue
                
                # Check if work is stale (needs to run)
                if not self._check_is_stale(work):
                    # Skip this work - output is already valid
                    self._skip_work(work)
                    continue
                
                service_name = task_type.service
                
                # Check service limits
                if service_name and not self._can_dispatch(service_name):
                    remaining.append(work)
                    continue
                
                # Mark for dispatch and track in service immediately
                # (so subsequent items in same loop see updated counts)
                if service_name:
                    self._service_active[service_name].add(work.id)
                    self._service_requests[service_name].append(time.time())
                
                to_dispatch.append(work)
            
            # Update queue with remaining work
            self._queue = remaining
            
            # Dispatch collected work
            for work in to_dispatch:
                work.state = WorkState.RUNNING
                work.started_at = time.time()
                self._active[work.id] = work
                
                # Create task to execute work
                task = asyncio.create_task(self._execute_work(work))
                self._work_tasks[work.id] = task
            
            # Small sleep to avoid busy loop
            await asyncio.sleep(0.01)
    
    def _check_is_ready(self, work: WorkUnit) -> bool:
        """Check if work is ready to run. Returns True if no callback registered."""
        if self._is_ready_callback is None:
            return True
        try:
            return bool(self._is_ready_callback(work))
        except Exception:
            # Exception in callback = treat as not ready
            return False
    
    def _check_is_stale(self, work: WorkUnit) -> bool:
        """Check if work output is stale. Returns True if no callback registered."""
        if self._is_stale_callback is None:
            return True  # Default: always stale, always run
        try:
            return bool(self._is_stale_callback(work))
        except Exception:
            # Exception in callback = treat as stale (run the work)
            return True
    
    def _get_priority(self, work: WorkUnit, queue_depth: int) -> float:
        """Get priority for work. Returns 0.5 if no callback registered."""
        if self._priority_callback is None:
            # Default: FIFO with starvation prevention
            # Older items get slightly higher priority (max 0.9)
            wait_time = time.time() - work.created_at
            return min(0.3 + wait_time / 3600, 0.9)
        
        try:
            ctx = PriorityContext(
                work=work,
                wait_time=time.time() - work.created_at,
                queue_depth=queue_depth,
            )
            priority = float(self._priority_callback(ctx))
            # Clamp to 0.0-1.0
            return max(0.0, min(1.0, priority))
        except Exception:
            # Exception in callback = default priority
            return 0.5
    
    def _skip_work(self, work: WorkUnit) -> None:
        """Skip work unit without running handler."""
        work.state = WorkState.COMPLETED  # Mark as completed (output valid)
        work.completed_at = time.time()
        self._completed[work.id] = work
        
        # Emit on_skip callback
        if self._on_skip_callback:
            try:
                self._on_skip_callback(work)
            except Exception:
                pass  # Don't let callback errors affect flow
    
    def _can_dispatch(self, service_name: str) -> bool:
        """Check if service limits allow dispatching more work."""
        service = self._services.get(service_name)
        if service is None:
            return True
        
        # Check concurrent limit
        concurrent_limit = service.get("concurrent")
        if concurrent_limit is not None:
            active_count = len(self._service_active.get(service_name, set()))
            if active_count >= concurrent_limit:
                return False
        
        # Check rate limit
        rate_limit = service.get("rate_limit")
        rate_window = service.get("rate_window")
        if rate_limit is not None and rate_window is not None:
            now = time.time()
            window_start = now - rate_window
            
            # Clean old timestamps and count recent requests
            timestamps = self._service_requests.get(service_name, [])
            recent = [t for t in timestamps if t > window_start]
            self._service_requests[service_name] = recent
            
            if len(recent) >= rate_limit:
                return False
        
        return True
    
    async def _execute_work(self, work: WorkUnit) -> None:
        """Execute a single work unit."""
        task_type = self._tasks.get(work.task)
        service_name = task_type.service if task_type else None
        
        if task_type is None or task_type.handler is None:
            work.state = WorkState.FAILED
            work.error = f"No handler for task: {work.task}"
            work.completed_at = time.time()
            self._active.pop(work.id, None)
            self._completed[work.id] = work
            self._work_tasks.pop(work.id, None)
            # Release service slot
            if service_name and service_name in self._service_active:
                self._service_active[service_name].discard(work.id)
            return
        
        handler = task_type.handler
        start_time = time.time()
        
        # Emit on_start callback
        if self._on_start_callback:
            try:
                self._on_start_callback(work)
            except Exception:
                pass  # Don't let callback errors affect flow
        
        try:
            # Call handler (sync or async)
            if inspect.iscoroutinefunction(handler):
                result = await handler(work)
            else:
                result = handler(work)
            
            # Success
            duration = time.time() - start_time
            work.state = WorkState.COMPLETED
            work.result = result
            work.completed_at = time.time()
            
            # Emit on_complete callback
            if self._on_complete_callback:
                try:
                    self._on_complete_callback(work, result, duration)
                except Exception:
                    pass  # Don't let callback errors affect flow
            
        except Exception as e:
            # Failure
            duration = time.time() - start_time
            work.state = WorkState.FAILED
            work.error = str(e)
            work.completed_at = time.time()
            
            # Emit on_failure callback
            if self._on_failure_callback:
                try:
                    self._on_failure_callback(work, e)
                except Exception:
                    pass  # Don't let callback errors affect flow
        
        finally:
            # Move from active to completed
            self._active.pop(work.id, None)
            self._completed[work.id] = work
            self._work_tasks.pop(work.id, None)
            # Release service slot
            if service_name and service_name in self._service_active:
                self._service_active[service_name].discard(work.id)
    
    # --- Work Operations ---
    
    async def submit(
        self,
        task: str,
        *,
        params: dict[str, Any] | None = None,
    ) -> str:
        """
        Submit work to the queue.
        
        Args:
            task: Name of registered task type.
            params: Parameters for the handler.
            
        Returns:
            Work unit ID.
            
        Raises:
            ValueError: If task is not registered.
        """
        if task not in self._tasks:
            raise ValueError(f"Unknown task: {task}")
        
        work_id = uuid.uuid4().hex[:12]
        work = WorkUnit(
            id=work_id,
            task=task,
            params=params or {},
            state=WorkState.PENDING,
            created_at=time.time(),
        )
        self._queue.append(work)
        return work_id
    
    async def get(self, work_id: str) -> WorkUnit | None:
        """
        Get a work unit by ID.
        
        Searches pending queue, active work, and completed work.
        
        Returns:
            WorkUnit if found, None otherwise.
        """
        # Check pending queue
        for work in self._queue:
            if work.id == work_id:
                return work
        
        # Check active work
        if work_id in self._active:
            return self._active[work_id]
        
        # Check completed work
        if work_id in self._completed:
            return self._completed[work_id]
        
        return None
    
    async def list(
        self,
        *,
        state: WorkState | None = None,
        task: str | None = None,
        limit: int = 100,
    ) -> list[WorkUnit]:
        """
        List work units with optional filters.
        
        Args:
            state: Filter by work state.
            task: Filter by task type name.
            limit: Maximum number of results.
            
        Returns:
            List of matching work units.
        """
        # Collect all work from all storage
        all_work: list[WorkUnit] = []
        all_work.extend(self._queue)
        all_work.extend(self._active.values())
        all_work.extend(self._completed.values())
        
        # Apply filters
        result = []
        for work in all_work:
            if state is not None and work.state != state:
                continue
            if task is not None and work.task != task:
                continue
            result.append(work)
            if len(result) >= limit:
                break
        
        return result
    
    async def cancel(self, work_id: str) -> bool:
        """
        Cancel a work unit.
        
        Args:
            work_id: ID of work to cancel.
            
        Returns:
            True if work was cancelled, False if not found or already completed.
        """
        # Check pending queue
        for i, work in enumerate(self._queue):
            if work.id == work_id:
                work.state = WorkState.CANCELLED
                work.completed_at = time.time()
                self._queue.pop(i)
                self._completed[work_id] = work
                return True
        
        # TODO: Handle cancelling running work in Phase 2
        # For now, can't cancel active work
        if work_id in self._active:
            return False
        
        # Already completed/failed/cancelled
        return False
    
    def debug_blocked(self) -> list[dict[str, Any]]:
        """
        Get diagnostic info about why queued work is blocked.
        
        Returns a list of dicts with:
        - work: The WorkUnit
        - reason: Why it's blocked ('not_ready', 'service_full', 'unknown_task')
        - details: Additional context
        
        Example:
            for item in cue.debug_blocked():
                print(f"{item['work'].task}: {item['reason']} - {item['details']}")
        """
        blocked = []
        
        for work in self._queue:
            reason = "unknown"
            details = ""
            
            # Check task registration
            task_type = self._tasks.get(work.task)
            if task_type is None:
                reason = "unknown_task"
                details = f"Task '{work.task}' not registered"
                blocked.append({"work": work, "reason": reason, "details": details})
                continue
            
            # Check is_ready
            if not self._check_is_ready(work):
                reason = "not_ready"
                details = f"is_ready returned False for {work.task}(params={work.params})"
                blocked.append({"work": work, "reason": reason, "details": details})
                continue
            
            # Check service capacity
            service_name = task_type.service
            if service_name and not self._can_dispatch(service_name):
                reason = "service_full"
                svc = self._services.get(service_name)
                if svc:
                    active = len(self._service_active.get(service_name, set()))
                    details = f"Service '{service_name}' at capacity ({active}/{svc.concurrent})"
                else:
                    details = f"Service '{service_name}' not configured"
                blocked.append({"work": work, "reason": reason, "details": details})
                continue
        
        return blocked
