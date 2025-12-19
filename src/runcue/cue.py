"""Core Cue orchestrator class."""

from __future__ import annotations

from typing import Any, Callable

from runcue.models import TaskType, WorkState, WorkUnit


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
        
        # In-memory work queue (to be implemented in Phase 1)
        self._queue: list[WorkUnit] = []
        self._active: dict[str, WorkUnit] = {}
        
        # Rate limit tracking (to be implemented in Phase 3)
        self._service_active: dict[str, set[str]] = {}
        self._service_requests: dict[str, list[float]] = {}
        
        # Orchestrator state
        self._running = False
    
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
            retry: Maximum attempts before failing.
        
        Example:
            @cue.task("extract", uses="openai", retry=3)
            def extract(work):
                return {"text": call_api(work.params["input"])}
        """
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
        
        Called after failure with (work, error, will_retry).
        
        Example:
            @cue.on_failure
            def on_failure(work, error, will_retry):
                if not will_retry:
                    alerting.send(f"Failed: {work.task}")
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
        # TODO: Implement in Phase 2
        self._running = True
    
    async def stop(self) -> None:
        """
        Stop the orchestrator gracefully.
        
        Waits for currently running work to complete.
        """
        # TODO: Implement in Phase 2
        self._running = False
    
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
        """
        # TODO: Implement in Phase 1
        raise NotImplementedError("submit() not yet implemented")
    
    async def get(self, work_id: str) -> WorkUnit | None:
        """Get a work unit by ID."""
        # TODO: Implement in Phase 1
        raise NotImplementedError("get() not yet implemented")
    
    async def list(
        self,
        *,
        state: WorkState | None = None,
        task: str | None = None,
        limit: int = 100,
    ) -> list[WorkUnit]:
        """List work units with optional filters."""
        # TODO: Implement in Phase 1
        raise NotImplementedError("list() not yet implemented")
