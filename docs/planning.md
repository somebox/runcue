# runcue Development Plan

A phased approach to building runcue incrementally, with passing tests at each stage.

## Principles

1. **Small phases** — Each phase is independently testable and useful
2. **Tests first** — Write tests that define expected behavior, then implement
3. **Foundation before features** — Get core scheduling solid before adding complexity
4. **Artifact-based** — Dependencies check artifacts, not work completion
5. **Stateless** — No persistence, artifacts are the source of truth

---

## Architecture Summary

runcue is a **control tower** for scheduling work:

- **runcue decides when** — Checks readiness, enforces rate limits, calls handlers
- **You decide what** — Handlers do the work, you own artifacts
- **Artifacts are proof** — Tasks produce evidence of completion
- **No persistence** — Everything in-memory, artifacts are truth

Key callbacks the client provides:

| Callback | Question | Returns |
|----------|----------|---------|
| `@cue.is_ready` | "Are inputs valid?" | `bool` |
| `@cue.is_stale` | "Is output missing/outdated?" | `bool` |
| `@cue.priority` | "How urgent is this?" | `float` 0.0-1.0 |
| `@cue.on_complete` | (event) Work completed | — |
| `@cue.on_failure` | (event) Work failed | — |
| `@cue.on_skip` | (event) Work skipped (not stale) | — |
| `@cue.on_start` | (event) Work starting | — |

---

## Design Clarifications

### Stateless Design

runcue tracks work state in-memory during execution, but **artifacts are the source of truth** for determining whether work needs to run:

- `is_stale(work)` returns `False` → work is skipped (handler not called)
- `on_skip` callback fires so clients can log/track if desired
- No separate `SKIPPED` state—the artifact's existence/validity is what matters
- On restart, resubmit work; `is_stale` will skip anything already complete

### Work States

```python
class WorkState(Enum):
    PENDING = "pending"     # Queued, waiting to run
    RUNNING = "running"     # Handler currently executing
    COMPLETED = "completed" # Handler returned successfully
    FAILED = "failed"       # Handler raised exception
    CANCELLED = "cancelled" # Stopped before completion
```

### Sync and Async Handlers

Handlers may be sync or async. The orchestrator detects `inspect.iscoroutinefunction()` and awaits async handlers appropriately:

```python
# Sync handler
@cue.task("fast", uses="local")
def fast(work):
    return {"done": True}

# Async handler
@cue.task("slow", uses="api")
async def slow(work):
    result = await external_api(work.params["url"])
    return {"result": result}
```

### Submit Before Start

Work can be submitted before `start()` is called. It queues and waits until the orchestrator starts:

```python
cue = runcue.Cue()
# ... register tasks ...
work_id = await cue.submit("task", params={})  # Queues immediately
cue.start()  # Now work begins executing
```

### No Built-in Retry

Retry logic is highly use-case dependent (backoff strategy, jitter, circuit breakers). Instead of building retry into runcue:

- `on_failure` callback notifies clients of failures
- Clients decide whether to resubmit (with their own delay/logic)
- This keeps runcue simple and gives clients full control

---

## Current Status

**Phase 3: Complete** — 50 tests passing

| Phase | Status | Tests |
|-------|--------|-------|
| Phase 0: Project Setup | ✓ Complete | 13 |
| Phase 1: Data Models | ✓ Complete | 15 |
| Phase 2: Basic Execution | ✓ Complete | 13 |
| Phase 3: Rate Limiting | ✓ Complete | 9 |
| Phase 4: Readiness | Pending | — |

---

## Phase 0: Project Setup ✓

**Goal**: Establish project structure, tooling, and basic instantiation.

### Tasks

- [x] Clean up old implementation files
- [x] Update `__init__.py` exports
- [x] Create stub `Cue` class
- [x] Basic test infrastructure

### Tests (13 passing)

- `test_import`
- `test_cue_instantiation`
- `test_service_registration`
- `test_service_rate_formats`
- `test_service_no_rate`
- `test_task_registration`
- `test_is_ready_registration`
- `test_is_stale_registration`
- `test_priority_registration`
- `test_on_complete_registration`
- `test_on_failure_registration`
- `test_on_skip_registration`
- `test_on_start_registration`

---

## Phase 1: Data Models (In-Memory) ✓

**Goal**: Define core data structures. In-memory queue, no persistence.

### Models

```python
@dataclass
class WorkUnit:
    id: str
    task: str           # Task type name
    params: dict        # All parameters
    state: WorkState    # pending, running, completed, failed, cancelled
    created_at: float
    started_at: float | None
    completed_at: float | None
    result: dict | None
    error: str | None
    attempt: int        # Always 1 (no built-in retry)

class WorkState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
```

### In-Memory Storage

```python
# In Cue class
self._queue: list[WorkUnit] = []           # Pending work
self._active: dict[str, WorkUnit] = {}     # Running work
self._completed: dict[str, WorkUnit] = {}  # Completed/failed/cancelled
```

### Tasks

- [x] Implement `submit()` — create work, add to queue
- [x] Implement `get()` — retrieve work by ID
- [x] Implement `list()` — query work by state/task
- [x] Implement `cancel()` — cancel pending or running work
- [x] Generate unique work IDs
- [x] Validate task exists on submit
- [x] Validate service exists on task registration

### Tests

```python
async def test_submit_returns_id():
    cue = runcue.Cue()
    cue.service("api", rate="60/min")
    
    @cue.task("process", uses="api")
    def process(work):
        return {}
    
    work_id = await cue.submit("process", params={"x": 1})
    assert work_id is not None

async def test_get_returns_work():
    cue = runcue.Cue()
    cue.service("api", rate="60/min")
    
    @cue.task("process", uses="api")
    def process(work):
        return {}
    
    work_id = await cue.submit("process", params={"x": 1})
    work = await cue.get(work_id)
    assert work.params == {"x": 1}
    assert work.state == WorkState.PENDING

async def test_list_filters_by_state():
    cue = runcue.Cue()
    cue.service("api", rate="60/min")
    
    @cue.task("process", uses="api")
    def process(work):
        return {}
    
    await cue.submit("process", params={"x": 1})
    await cue.submit("process", params={"x": 2})
    
    pending = await cue.list(state=WorkState.PENDING)
    assert len(pending) == 2

async def test_submit_unknown_task_raises():
    cue = runcue.Cue()
    with pytest.raises(ValueError, match="Unknown task"):
        await cue.submit("nonexistent", params={})

def test_task_unknown_service_raises():
    cue = runcue.Cue()
    with pytest.raises(ValueError, match="Unknown service"):
        @cue.task("bad", uses="nonexistent")
        def bad(work):
            pass
```

---

## Phase 2: Basic Execution ✓

**Goal**: Execute work. All pending work runs (no readiness checks yet).

### Tasks

- [x] Implement orchestrator loop (background asyncio task)
- [x] `cue.start()` — non-blocking, starts loop
- [x] `await cue.stop(timeout=None)` — graceful shutdown with optional timeout
- [x] Pick pending work, call handler, update state
- [x] Support both sync and async handlers
- [x] Handle exceptions (mark failed)
- [x] `cancel()` already implemented in Phase 1

### Lifecycle

```python
cue.start()              # Start orchestrator (non-blocking)
await cue.stop()         # Wait for running work to complete
await cue.stop(timeout=5) # Wait up to 5 seconds, then abandon
```

### Tests

```python
async def test_work_executes():
    cue = runcue.Cue()
    cue.service("api", rate="100/min", concurrent=10)
    
    executed = []
    
    @cue.task("process", uses="api")
    def process(work):
        executed.append(work.id)
        return {"done": True}
    
    cue.start()
    work_id = await cue.submit("process", params={})
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert work_id in executed

async def test_async_handler_works():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    executed = []
    
    @cue.task("async_task", uses="api")
    async def async_task(work):
        await asyncio.sleep(0.01)
        executed.append(work.id)
        return {"done": True}
    
    cue.start()
    work_id = await cue.submit("async_task", params={})
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert work_id in executed

async def test_handler_exception_marks_failed():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    @cue.task("failing", uses="api")
    def failing(work):
        raise ValueError("oops")
    
    cue.start()
    work_id = await cue.submit("failing", params={})
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    work = await cue.get(work_id)
    assert work.state == WorkState.FAILED
    assert "oops" in work.error

async def test_stop_waits_for_running():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    completed = []
    
    @cue.task("slow", uses="api")
    async def slow(work):
        await asyncio.sleep(0.2)
        completed.append(work.id)
        return {}
    
    cue.start()
    work_id = await cue.submit("slow", params={})
    await asyncio.sleep(0.05)  # Let it start
    await cue.stop()  # Should wait for completion
    
    assert work_id in completed

async def test_stop_timeout_abandons():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    @cue.task("very_slow", uses="api")
    async def very_slow(work):
        await asyncio.sleep(10)
        return {}
    
    cue.start()
    await cue.submit("very_slow", params={})
    await asyncio.sleep(0.05)
    
    start = time.time()
    await cue.stop(timeout=0.1)
    elapsed = time.time() - start
    
    assert elapsed < 1  # Didn't wait for the 10s task

async def test_cancel_pending_work():
    cue = runcue.Cue()
    cue.service("api", concurrent=1, rate="100/min")
    
    @cue.task("task", uses="api")
    async def task(work):
        await asyncio.sleep(1)
        return {}
    
    cue.start()
    # Submit two - first will run, second will be pending
    await cue.submit("task", params={})
    work_id2 = await cue.submit("task", params={})
    await asyncio.sleep(0.05)
    
    await cue.cancel(work_id2)
    work = await cue.get(work_id2)
    assert work.state == WorkState.CANCELLED
    
    await cue.stop(timeout=0.1)

async def test_submit_before_start_queues():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    executed = []
    
    @cue.task("task", uses="api")
    def task(work):
        executed.append(work.id)
        return {}
    
    # Submit before start
    work_id = await cue.submit("task", params={})
    work = await cue.get(work_id)
    assert work.state == WorkState.PENDING
    assert work_id not in executed
    
    # Now start
    cue.start()
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert work_id in executed
```

---

## Phase 3: Rate Limiting & Concurrency ✓

**Goal**: Respect service limits (in-memory tracking).

### Tasks

- [x] Track active work per service
- [x] Track request timestamps per service (for rate windows)
- [x] Check concurrent limit before dispatch
- [x] Check rate limit before dispatch
- [x] Queue work when limits exceeded (re-check each loop)

### In-Memory Tracking

```python
# Per service
self._service_active: dict[str, set[str]] = {}  # service -> active work IDs
self._service_requests: dict[str, list[float]] = {}  # service -> request timestamps
```

### Tests

```python
async def test_max_concurrent_respected():
    cue = runcue.Cue()
    cue.service("api", concurrent=2, rate="1000/min")
    
    running = 0
    max_running = 0
    
    @cue.task("slow", uses="api")
    async def slow(work):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.02)
        running -= 1
        return {}
    
    cue.start()
    for _ in range(6):
        await cue.submit("slow", params={})
    
    await asyncio.sleep(0.15)
    await cue.stop()
    
    assert max_running <= 2

async def test_rate_limit_throttles():
    cue = runcue.Cue()
    cue.service("api", rate="3/sec", concurrent=100)
    
    timestamps = []
    
    @cue.task("record", uses="api")
    def record(work):
        timestamps.append(time.time())
        return {}
    
    cue.start()
    for _ in range(6):
        await cue.submit("record", params={})
    
    await asyncio.sleep(1.5)
    await cue.stop()
    
    # First 3 run immediately, wait ~1s, then next 3
    assert len(timestamps) == 6
    duration = timestamps[-1] - timestamps[0]
    assert duration >= 0.9
```

---

## Phase 4: Readiness Callback

**Goal**: `@cue.is_ready` gates work execution based on input availability.

### Context

The `is_ready` callback lets the client tell runcue whether a work unit's inputs are valid. This enables dependency chains without explicit task linking—work waits until its inputs exist.

```python
@cue.is_ready
def is_ready(work) -> bool:
    if work.task == "process_page":
        # Wait until the page image exists
        return Path(work.params["page_path"]).exists()
    return True  # Default: always ready
```

### Tasks

- [ ] Call `is_ready(work)` before dispatch in `_run_orchestrator()`
- [ ] If no callback registered, default to `True` (always ready)
- [ ] Work stays in queue if `is_ready` returns `False`
- [ ] Handle `is_ready` exceptions (log warning, treat as not ready)
- [ ] Work is rechecked each orchestrator loop iteration

### Implementation Hints

**Note**: The `@cue.is_ready` decorator and `_is_ready_callback` storage already exist in `cue.py`—only the dispatch logic needs to be added.

In `_run_orchestrator()`, add the is_ready check after getting the task_type:

```python
for work in self._queue:
    task_type = self._tasks.get(work.task)
    if task_type is None:
        remaining.append(work)
        continue
    
    # NEW: Check if work is ready
    if not self._check_is_ready(work):
        remaining.append(work)
        continue
    
    service_name = task_type.service
    # ... rest of dispatch logic
```

Add helper method:

```python
def _check_is_ready(self, work: WorkUnit) -> bool:
    """Check if work is ready to run. Returns True if no callback registered."""
    if self._is_ready_callback is None:
        return True
    try:
        return bool(self._is_ready_callback(work))
    except Exception as e:
        # Log warning, treat as not ready
        return False
```

### Tests

```python
async def test_is_ready_blocks_work():
    """Work stays pending until is_ready returns True."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    ready_flag = False
    
    @cue.task("needs_flag", uses="api")
    def needs_flag(work):
        return {"done": True}
    
    @cue.is_ready
    def is_ready(work):
        return ready_flag
    
    cue.start()
    work_id = await cue.submit("needs_flag", params={})
    await asyncio.sleep(0.1)
    
    work = await cue.get(work_id)
    assert work.state == WorkState.PENDING  # Blocked
    
    ready_flag = True
    await asyncio.sleep(0.15)
    
    work = await cue.get(work_id)
    assert work.state == WorkState.COMPLETED  # Now runs
    
    await cue.stop()

async def test_no_is_ready_callback_allows_all():
    """Without is_ready callback, all work runs immediately."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    @cue.task("task", uses="api")
    def task(work):
        return {}
    
    # No @cue.is_ready registered
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    work = await cue.get(work_id)
    assert work.state == WorkState.COMPLETED

async def test_is_ready_exception_treated_as_not_ready():
    """Exceptions in is_ready are caught; work stays pending."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    @cue.task("task", uses="api")
    def task(work):
        return {}
    
    @cue.is_ready
    def is_ready(work):
        raise RuntimeError("check failed")
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    
    work = await cue.get(work_id)
    assert work.state == WorkState.PENDING  # Not run due to exception
    
    await cue.stop()

async def test_is_ready_checked_per_work():
    """is_ready is called for each work unit independently."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    ready_keys = set()
    
    @cue.task("task", uses="api")
    def task(work):
        return {}
    
    @cue.is_ready
    def is_ready(work):
        return work.params.get("key") in ready_keys
    
    cue.start()
    
    # Submit two work units
    id1 = await cue.submit("task", params={"key": "a"})
    id2 = await cue.submit("task", params={"key": "b"})
    await asyncio.sleep(0.1)
    
    # Both should be pending
    assert (await cue.get(id1)).state == WorkState.PENDING
    assert (await cue.get(id2)).state == WorkState.PENDING
    
    # Make only "a" ready
    ready_keys.add("a")
    await asyncio.sleep(0.15)
    
    # Only id1 should complete
    assert (await cue.get(id1)).state == WorkState.COMPLETED
    assert (await cue.get(id2)).state == WorkState.PENDING
    
    await cue.stop()
```

---

## Phase 5: Staleness Callback

**Goal**: `@cue.is_stale` skips work when output already exists/is valid.

### Context

The `is_stale` callback lets the client tell runcue whether a work unit's output needs to be (re)generated. If the output is fresh, the work is skipped without running the handler.

```python
@cue.is_stale
def is_stale(work) -> bool:
    if work.task == "extract_text":
        output = work.params["image_path"].replace(".png", ".txt")
        return not Path(output).exists()  # Stale if output missing
    return True  # Default: always stale (run if ready)
```

### Tasks

- [ ] After `is_ready` passes, check `is_stale` in `_run_orchestrator()`
- [ ] If `is_stale` returns `False`, skip work (don't run handler, don't track in service)
- [ ] Emit `on_skip` callback for skipped work
- [ ] If no callback registered, default to `True` (always stale, always run)
- [ ] Handle `is_stale` exceptions (log warning, treat as stale—run the work)

### Implementation Hints

**Note**: The `@cue.is_stale` decorator and `_is_stale_callback` storage already exist in `cue.py`—only the dispatch logic needs to be added.

In `_run_orchestrator()`, after the is_ready check:

```python
# Check if work is ready
if not self._check_is_ready(work):
    remaining.append(work)
    continue

# NEW: Check if work is stale (needs to run)
if not self._check_is_stale(work):
    # Skip this work - output is already valid
    self._skip_work(work)
    continue

service_name = task_type.service
# ... rest of dispatch logic
```

Add helper methods:

```python
def _check_is_stale(self, work: WorkUnit) -> bool:
    """Check if work output is stale. Returns True if no callback registered."""
    if self._is_stale_callback is None:
        return True  # Default: always stale, always run
    try:
        return bool(self._is_stale_callback(work))
    except Exception as e:
        # Log warning, treat as stale (run the work)
        return True

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
```

### Tests

```python
async def test_is_stale_skips_work():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    executed = []
    stale = True
    
    @cue.task("extract", uses="api")
    def extract(work):
        executed.append(work.id)
        return {}
    
    @cue.is_stale
    def is_stale(work):
        return stale
    
    # First run: stale=True, should execute
    cue.start()
    work_id = await cue.submit("extract", params={})
    await asyncio.sleep(0.1)
    assert work_id in executed
    
    # Second run: stale=False, should skip
    stale = False
    executed.clear()
    work_id2 = await cue.submit("extract", params={})
    await asyncio.sleep(0.1)
    assert work_id2 not in executed
    
    await cue.stop()

async def test_no_is_stale_callback_runs_all():
    """Without is_stale callback, all ready work runs."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    executed = []
    
    @cue.task("task", uses="api")
    def task(work):
        executed.append(work.id)
        return {}
    
    # No @cue.is_stale registered
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert work_id in executed

async def test_is_stale_exception_treated_as_stale():
    """Exceptions in is_stale are caught; work runs (treat as stale)."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    executed = []
    
    @cue.task("task", uses="api")
    def task(work):
        executed.append(work.id)
        return {}
    
    @cue.is_stale
    def is_stale(work):
        raise RuntimeError("check failed")
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    # Work should run despite exception (exception = treat as stale)
    assert work_id in executed

async def test_on_skip_called():
    """on_skip callback fires when work is skipped."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    skipped = []
    
    @cue.task("task", uses="api")
    def task(work):
        return {}
    
    @cue.is_stale
    def is_stale(work):
        return False  # Not stale, should skip
    
    @cue.on_skip
    def on_skip(work):
        skipped.append(work.id)
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert work_id in skipped

async def test_skipped_work_marked_completed():
    """Skipped work should have COMPLETED state and completed_at timestamp."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    @cue.task("task", uses="api")
    def task(work):
        return {}
    
    @cue.is_stale
    def is_stale(work):
        return False  # Not stale, should skip
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    work = await cue.get(work_id)
    assert work.state == WorkState.COMPLETED
    assert work.completed_at is not None
```

---

## Phase 6: Event Callbacks

**Goal**: Callbacks for history and metrics.

### Tasks

- [ ] Implement `@cue.on_complete(work, result, duration)`
- [ ] Implement `@cue.on_failure(work, error)`
- [ ] Implement `@cue.on_skip(work)` (already in Phase 5)
- [ ] Implement `@cue.on_start(work)`
- [ ] Track duration per work unit

### Tests

```python
async def test_on_complete_called():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    completions = []
    
    @cue.task("task", uses="api")
    def task(work):
        return {"x": 1}
    
    @cue.on_complete
    def on_complete(work, result, duration):
        completions.append((work.id, result, duration))
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert len(completions) == 1
    assert completions[0][0] == work_id
    assert completions[0][1] == {"x": 1}
    assert completions[0][2] >= 0  # duration

async def test_on_failure_called():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    failures = []
    
    @cue.task("task", uses="api")
    def task(work):
        raise ValueError("boom")
    
    @cue.on_failure
    def on_failure(work, error):
        failures.append((work.id, str(error)))
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert len(failures) == 1
    assert failures[0][0] == work_id
    assert "boom" in failures[0][1]

async def test_on_start_called():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    starts = []
    
    @cue.task("task", uses="api")
    def task(work):
        return {}
    
    @cue.on_start
    def on_start(work):
        starts.append(work.id)
    
    cue.start()
    work_id = await cue.submit("task", params={})
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert work_id in starts
```

---

## Phase 7: Priority Callback

**Goal**: `@cue.priority` controls scheduling order.

### PriorityContext Model

```python
@dataclass
class PriorityContext:
    work: WorkUnit
    wait_time: float      # Seconds since created_at
    queue_depth: int      # Total pending work count
```

### Tasks

- [ ] Provide context (work, wait_time, queue_depth)
- [ ] Returns 0.0-1.0, higher runs first
- [ ] Default: FIFO with starvation prevention

### Tests

```python
async def test_priority_ordering():
    cue = runcue.Cue()
    cue.service("api", concurrent=1, rate="100/min")
    
    order = []
    
    @cue.task("task", uses="api")
    async def task(work):
        order.append(work.params["name"])
        await asyncio.sleep(0.01)
        return {}
    
    @cue.priority
    def prioritize(ctx):
        return ctx.work.params.get("priority", 0.5)
    
    cue.start()
    
    await cue.submit("task", params={"name": "low", "priority": 0.1})
    await cue.submit("task", params={"name": "high", "priority": 0.9})
    await cue.submit("task", params={"name": "med", "priority": 0.5})
    
    await asyncio.sleep(0.5)
    await cue.stop()
    
    assert order[0] == "high"

async def test_priority_context_has_wait_time():
    cue = runcue.Cue()
    cue.service("api", concurrent=1, rate="100/min")
    
    wait_times = []
    
    @cue.task("task", uses="api")
    async def task(work):
        await asyncio.sleep(0.1)
        return {}
    
    @cue.priority
    def prioritize(ctx):
        wait_times.append(ctx.wait_time)
        return 0.5
    
    cue.start()
    await cue.submit("task", params={})
    await cue.submit("task", params={})
    
    await asyncio.sleep(0.5)
    await cue.stop()
    
    # Second task should have waited longer
    assert len(wait_times) >= 2
```

---

## Phase 8: Polish

**Goal**: Complete API, edge cases.

### Tasks

- [ ] Comprehensive error messages
- [ ] Edge case handling (empty queue, rapid start/stop)
- [ ] Performance optimization
- [ ] Documentation examples
- [ ] Integration test with full pipeline

### Integration Test

```python
async def test_full_pipeline():
    """Integration test: submit → is_ready → is_stale → execute → callbacks."""
    cue = runcue.Cue()
    cue.service("api", rate="100/min", concurrent=2)
    
    artifacts = {}
    events = []
    
    @cue.task("produce", uses="api")
    def produce(work):
        artifacts[work.params["key"]] = "data"
        return {"key": work.params["key"]}
    
    @cue.task("consume", uses="api")
    def consume(work):
        return {"value": artifacts[work.params["key"]]}
    
    @cue.is_ready
    def is_ready(work):
        if work.task == "consume":
            return work.params["key"] in artifacts
        return True
    
    @cue.is_stale
    def is_stale(work):
        if work.task == "produce":
            return work.params["key"] not in artifacts
        return True
    
    @cue.on_complete
    def on_complete(work, result, duration):
        events.append(("complete", work.task))
    
    cue.start()
    
    # Submit consumer first (will wait for producer)
    await cue.submit("consume", params={"key": "x"})
    await asyncio.sleep(0.1)
    assert len(events) == 0  # Blocked by is_ready
    
    # Submit producer
    await cue.submit("produce", params={"key": "x"})
    await asyncio.sleep(0.3)
    
    assert ("complete", "produce") in events
    assert ("complete", "consume") in events
    
    await cue.stop()
```

---

## Dependency Graph

```text
Phase 0: Setup ✓
    │
    ▼
Phase 1: Models
    │
    ▼
Phase 2: Execution
    │
    ├────────────────┐
    ▼                ▼
Phase 3:         Phase 4:
Rate Limiting    is_ready
    │                │
    └────────┬───────┘
             ▼
         Phase 5: is_stale
             │
             ▼
         Phase 6: Callbacks
             │
             ▼
         Phase 7: Priority
             │
             ▼
         Phase 8: Polish
```

---

## Milestones

| Milestone | Phases | Capability |
|-----------|--------|------------|
| **M1: Minimal** | 0-2 | Submit work, see it execute |
| **M2: Controlled** | 3-5 | Rate limits, readiness, staleness |
| **M3: Observable** | 6-7 | Callbacks, priority |
| **M4: Complete** | 8 | Polished, documented |

---

## Notes

- Each phase should take 1-2 days
- Tests written first, then implementation
- No database — fully in-memory
- Artifacts are the source of truth
- No built-in retry — clients handle failures via callbacks
- Community can add persistence via callbacks
