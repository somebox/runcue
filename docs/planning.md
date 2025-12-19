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

---

## Current Status

**Phase 0: Complete** — 11 tests passing

| Phase | Status | Tests |
|-------|--------|-------|
| Phase 0: Project Setup | ✓ Complete | 11 |
| Phase 1: Data Models | Pending | — |
| Phase 2: Basic Execution | Pending | — |

---

## Phase 0: Project Setup ✓

**Goal**: Establish project structure, tooling, and basic instantiation.

### Tasks

- [x] Clean up old implementation files
- [x] Update `__init__.py` exports
- [x] Create stub `Cue` class
- [x] Basic test infrastructure

### Tests (11 passing)

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

---

## Phase 1: Data Models (In-Memory)

**Goal**: Define core data structures. In-memory queue, no persistence.

### Models

```python
@dataclass
class WorkUnit:
    id: str
    task: str           # Task type name
    params: dict        # All parameters
    state: WorkState    # pending, running, completed, failed
    created_at: float
    started_at: float | None
    completed_at: float | None
    result: dict | None
    error: str | None
    attempt: int        # Current attempt (1, 2, 3...)

class WorkState(Enum):
    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
```

### In-Memory Storage

```python
# In Cue class
self._queue: list[WorkUnit] = []           # Pending work
self._active: dict[str, WorkUnit] = {}     # Running work
```

### Tasks

- [ ] Implement `submit()` — create work, add to queue
- [ ] Implement `get()` — retrieve work by ID
- [ ] Implement `list()` — query work by state/task
- [ ] Generate unique work IDs

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
    # ... setup ...
    work_id = await cue.submit("process", params={"x": 1})
    work = await cue.get(work_id)
    assert work.params == {"x": 1}
    assert work.state == WorkState.PENDING
```

---

## Phase 2: Basic Execution

**Goal**: Execute work. All pending work runs (no readiness checks yet).

### Tasks

- [ ] Implement orchestrator loop (background asyncio task)
- [ ] `cue.start()` — non-blocking, starts loop
- [ ] `await cue.stop()` — graceful shutdown
- [ ] Pick pending work, call handler, update state
- [ ] Handle exceptions (mark failed)

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

async def test_handler_exception_marks_failed():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    @cue.task("failing", uses="api")
    def failing(work):
        raise ValueError("oops")
    
    cue.start()
    work_id = await cue.submit("failing")
    
    await asyncio.sleep(0.1)
    await cue.stop()
    
    work = await cue.get(work_id)
    assert work.state == WorkState.FAILED
    assert "oops" in work.error
```

---

## Phase 3: Rate Limiting & Concurrency

**Goal**: Respect service limits (in-memory tracking).

### Tasks

- [ ] Track active work per service
- [ ] Track request timestamps per service (for rate windows)
- [ ] Check concurrent limit before dispatch
- [ ] Check rate limit before dispatch
- [ ] Block work until limits allow

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
    cue.service("api", concurrent=2, rate="100/min")
    
    running = 0
    max_running = 0
    
    @cue.task("slow", uses="api")
    async def slow(work):
        nonlocal running, max_running
        running += 1
        max_running = max(max_running, running)
        await asyncio.sleep(0.05)
        running -= 1
        return {}
    
    cue.start()
    for _ in range(10):
        await cue.submit("slow")
    
    await asyncio.sleep(1)
    await cue.stop()
    
    assert max_running <= 2

async def test_rate_limit_respected():
    cue = runcue.Cue()
    cue.service("api", rate="5/sec", concurrent=10)
    
    # Submit 10, should take ~1 second (5 per sec)
    cue.start()
    for _ in range(10):
        await cue.submit("task")
    
    start = time.time()
    await asyncio.sleep(2.5)
    await cue.stop()
    
    elapsed = time.time() - start
    assert elapsed >= 1.5  # Rate limited
```

---

## Phase 4: Readiness Callback

**Goal**: `@cue.is_ready` gates work execution.

### Tasks

- [ ] Register `@cue.is_ready` callback
- [ ] Call `is_ready(work)` before dispatch
- [ ] Work stays pending if `is_ready` returns `False`
- [ ] Periodic recheck of pending work

### Tests

```python
async def test_is_ready_blocks_work():
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
    work_id = await cue.submit("needs_flag")
    await asyncio.sleep(0.1)
    
    work = await cue.get(work_id)
    assert work.state == WorkState.PENDING  # Blocked
    
    ready_flag = True
    await asyncio.sleep(0.2)
    
    work = await cue.get(work_id)
    assert work.state == WorkState.COMPLETED  # Now runs
    
    await cue.stop()
```

---

## Phase 5: Staleness Callback

**Goal**: `@cue.is_stale` skips work when output exists.

### Tasks

- [ ] Register `@cue.is_stale` callback
- [ ] After `is_ready` passes, check `is_stale`
- [ ] If `is_stale` returns `False`, skip work (mark completed without running)
- [ ] Emit `on_skip` callback for skipped work
- [ ] Default: always stale (always run)

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
    work_id = await cue.submit("extract")
    await asyncio.sleep(0.1)
    assert work_id in executed
    
    # Second run: stale=False, should skip
    stale = False
    executed.clear()
    work_id2 = await cue.submit("extract")
    await asyncio.sleep(0.1)
    assert work_id2 not in executed
    
    await cue.stop()
```

---

## Phase 6: Event Callbacks

**Goal**: Callbacks for history and metrics.

### Tasks

- [ ] Implement `@cue.on_complete(work, result, duration)`
- [ ] Implement `@cue.on_failure(work, error, will_retry)`
- [ ] Implement `@cue.on_skip(work)`
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
    work_id = await cue.submit("task")
    await asyncio.sleep(0.1)
    await cue.stop()
    
    assert len(completions) == 1
    assert completions[0][0] == work_id
    assert completions[0][1] == {"x": 1}
    assert completions[0][2] >= 0  # duration
```

---

## Phase 7: Priority Callback

**Goal**: `@cue.priority` controls scheduling order.

### Tasks

- [ ] Register `@cue.priority` callback
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
```

---

## Phase 8: Retry

**Goal**: Automatic retries with backoff.

### Tasks

- [ ] `retry=N` on task decorator
- [ ] Exponential backoff between attempts
- [ ] `work.attempt` tracks current attempt
- [ ] After max attempts, mark failed permanently
- [ ] `on_failure` receives `will_retry` flag

### Tests

```python
async def test_retry_on_failure():
    cue = runcue.Cue()
    cue.service("api", rate="100/min")
    
    attempts = []
    
    @cue.task("flaky", uses="api", retry=3)
    def flaky(work):
        attempts.append(work.attempt)
        if len(attempts) < 3:
            raise ValueError("not yet")
        return {"done": True}
    
    cue.start()
    work_id = await cue.submit("flaky")
    
    await asyncio.sleep(1)
    await cue.stop()
    
    assert len(attempts) == 3
    work = await cue.get(work_id)
    assert work.state == WorkState.COMPLETED
```

---

## Phase 9: Polish

**Goal**: Complete API, edge cases.

### Tasks

- [ ] Comprehensive error messages
- [ ] Edge case handling
- [ ] Performance optimization
- [ ] Documentation examples

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
         Phase 8: Retry
             │
             ▼
         Phase 9: Polish
```

---

## Milestones

| Milestone | Phases | Capability |
|-----------|--------|------------|
| **M1: Minimal** | 0-2 | Submit work, see it execute |
| **M2: Controlled** | 3-5 | Rate limits, readiness, staleness |
| **M3: Observable** | 6-7 | Callbacks, priority |
| **M4: Resilient** | 8 | Retry |
| **M5: Complete** | 9 | Polished, documented |

---

## Notes

- Each phase should take 1-2 days
- Tests written first, then implementation
- No database — fully in-memory
- Artifacts are the source of truth
- Community can add persistence via callbacks
