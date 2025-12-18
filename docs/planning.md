# runcue Development Plan

A phased approach to building runcue incrementally, with passing tests and an evolving simulator at each stage.

## Principles

1. **Small phases** — Each phase is independently testable and useful
2. **Simulator grows with library** — runcue-sim evolves to exercise new features
3. **Tests first** — Write tests that define expected behavior, then implement
4. **Foundation before features** — Get persistence and basic operations solid before adding complexity

---

## Current Status

**Milestone 1: Proof of Concept — COMPLETE ✓**

| Phase | Status | Tests |
|-------|--------|-------|
| Phase 0: Project Setup | ✓ Complete | 4 |
| Phase 1: Data Models & Persistence | ✓ Complete | 12 |
| Phase 2: Basic Queue Operations | ✓ Complete (merged into P1) | — |
| Phase 3: Async Executor | ✓ Complete | 11 |

**Total: 27 tests passing**

**Simulator: runcue-sim with Rich TUI ✓**
- Live progress display with Rich library
- Configurable latency, error rates, concurrency
- Service status tracking
- Recent events log
- Simple mode fallback (no Rich dependency)

**Next: Milestone 2 — Rate-Limited (Phases 4-5)**

---

## Phase 0: Project Setup ✓

**Goal**: Establish project structure, tooling, and CI.

### Tasks

- [x] Create Python package structure (`src/runcue/`)
- [x] Set up `pyproject.toml` with dependencies (aiosqlite, pytest, pytest-asyncio)
- [x] Create basic test infrastructure (`tests/`)
- [x] Add `.gitignore` for Python/SQLite artifacts
- [x] Create stub `Cue` class that can be imported

### Tests

```python
def test_import():
    import runcue
    assert hasattr(runcue, 'Cue')

def test_cue_instantiation():
    cue = runcue.Cue(":memory:")
    assert cue is not None
```

### Simulator

None yet — just verify project runs.

---

## Phase 1: Data Models & Persistence ✓

**Goal**: Define core data structures and SQLite schema. No execution yet.

### Tasks

- [x] Define `WorkUnit` dataclass (id, task_type, target, params, state, etc.)
- [x] Define `Service` dataclass (name, max_concurrent, rate_limit, rate_window)
- [x] Define `TaskType` dataclass (name, services, executor, handler)
- [x] Create SQLite schema for `work_units` table
- [x] Create SQLite schema for `services` table
- [x] Implement database initialization and migrations
- [x] Implement `cue.service()` to register services
- [x] Implement `@cue.task()` decorator to register task types

### Schema

```sql
CREATE TABLE work_units (
    id TEXT PRIMARY KEY,
    task_type TEXT NOT NULL,
    target TEXT,
    params TEXT,  -- JSON
    state TEXT DEFAULT 'queued',  -- queued, running, completed, failed, cancelled
    created_at REAL NOT NULL,
    started_at REAL,
    completed_at REAL,
    result TEXT,  -- JSON
    error TEXT,
    attempt INTEGER DEFAULT 1
);

CREATE TABLE services (
    name TEXT PRIMARY KEY,
    max_concurrent INTEGER,
    rate_limit INTEGER,
    rate_window INTEGER,
    circuit_state TEXT DEFAULT 'closed',
    circuit_failures INTEGER DEFAULT 0,
    circuit_last_failure REAL
);
```

### Tests

```python
async def test_service_registration():
    cue = runcue.Cue(":memory:")
    cue.service("openai", rate_limit=(60, 60), max_concurrent=5)
    # Verify service stored in DB

async def test_task_registration():
    cue = runcue.Cue(":memory:")
    
    @cue.task("extract", services=["openai"])
    async def extract(work):
        return {"done": True}
    
    # Verify task type registered

async def test_work_unit_persistence():
    cue = runcue.Cue("./test.db")
    work_id = await cue.submit("extract", target="/path/to/file")
    
    # Restart with same DB
    cue2 = runcue.Cue("./test.db")
    work = await cue2.get(work_id)
    assert work.state == "queued"
```

### Simulator (v0.1)

Basic harness that can create work units:

```bash
runcue-sim --count 100 --pattern none
# Creates 100 independent work units, no execution
# Output: "Created 100 work units in queue"
```

---

## Phase 2: Basic Queue Operations ✓

**Goal**: Submit work, retrieve status, no execution yet.

*Note: Implemented as part of Phase 1.*

### Tasks

- [x] Implement `cue.submit()` — creates work unit, returns ID
- [x] Implement `cue.get(work_id)` — retrieve work unit by ID
- [x] Implement `cue.list()` — query work units by state
- [x] Implement `idempotency_key` duplicate detection
- [x] Generate unique work IDs (UUID or similar)

### Tests

```python
async def test_submit_returns_id():
    cue = runcue.Cue(":memory:")
    cue.service("api", rate_limit=(10, 60))
    
    @cue.task("process", services=["api"])
    async def process(work):
        return {}
    
    work_id = await cue.submit("process", target="test")
    assert work_id is not None
    assert len(work_id) > 0

async def test_idempotency_key():
    cue = runcue.Cue(":memory:")
    # ... setup ...
    
    id1 = await cue.submit("process", target="a", idempotency_key="unique-1")
    id2 = await cue.submit("process", target="b", idempotency_key="unique-1")
    assert id1 == id2  # Same key returns existing

async def test_list_by_state():
    cue = runcue.Cue(":memory:")
    # ... submit several ...
    
    queued = await cue.list(state="queued")
    assert len(queued) == expected_count
```

### Simulator (v0.2)

Can now submit and query:

```bash
runcue-sim --count 100 --pattern none --stats
# Output:
# Submitted: 100
# Queued: 100
# Running: 0
# Completed: 0
```

---

## Phase 3: Async Executor (Single Task) ✓

**Goal**: Execute one work unit at a time with async handler. No rate limiting yet.

### Tasks

- [x] Implement orchestrator loop (background asyncio task)
- [x] Implement `cue.start()` — non-blocking, starts loop
- [x] Implement `await cue.stop()` — graceful shutdown
- [x] Pick one queued work unit, call handler, update state
- [x] Store result/error in work unit
- [x] Handle handler exceptions (mark failed)

### Tests

```python
async def test_work_executes():
    cue = runcue.Cue(":memory:")
    cue.service("api", rate_limit=(100, 60))
    
    executed = []
    
    @cue.task("process", services=["api"])
    async def process(work):
        executed.append(work.id)
        return {"processed": True}
    
    cue.start()
    work_id = await cue.submit("process", target="test")
    
    await asyncio.sleep(0.1)  # Let it run
    await cue.stop()
    
    assert work_id in executed
    work = await cue.get(work_id)
    assert work.state == "completed"
    assert work.result == {"processed": True}

async def test_handler_exception_marks_failed():
    # Handler raises → work.state == "failed", work.error set
```

### Simulator (v0.3) ✓

Rich TUI with live progress:

```bash
# Basic run with Rich TUI
runcue-sim --count 50 --latency 200

# With error injection
runcue-sim --count 100 --latency 100 --error-rate 0.1

# Set concurrency (not enforced yet - Phase 4)
runcue-sim --count 50 --latency 300 --concurrent 5

# Simple text mode (no Rich)
runcue-sim --count 30 --latency 100 --no-tui

# Verbose logging
runcue-sim --count 20 --latency 50 -v
```

Features:
- Live progress bar, throughput, queue stats
- Service status panel with concurrent/rate display
- Recent events log (started, completed, failed)
- Final summary table

---

## Phase 4: Concurrency & Rate Limiting

**Goal**: Respect `max_concurrent` and `rate_limit` per service.

### Tasks

- [ ] Create `service_log` table to track usage
- [ ] Implement concurrent limit check before dispatch
- [ ] Implement rate limit check (count requests in window)
- [ ] Execute multiple work units concurrently (up to limits)
- [ ] Implement service log pruning (configurable retention)

### Schema Addition

```sql
CREATE TABLE service_log (
    id INTEGER PRIMARY KEY,
    service TEXT NOT NULL,
    work_unit_id TEXT NOT NULL,
    started_at REAL NOT NULL,
    completed_at REAL
);

CREATE INDEX idx_service_log_service_time ON service_log(service, started_at);
```

### Tests

```python
async def test_max_concurrent_respected():
    cue = runcue.Cue(":memory:")
    cue.service("api", max_concurrent=2, rate_limit=(100, 60))
    
    running_count = 0
    max_running = 0
    
    @cue.task("slow", services=["api"])
    async def slow(work):
        nonlocal running_count, max_running
        running_count += 1
        max_running = max(max_running, running_count)
        await asyncio.sleep(0.1)
        running_count -= 1
        return {}
    
    cue.start()
    for _ in range(10):
        await cue.submit("slow")
    
    await asyncio.sleep(1)  # Let all complete
    await cue.stop()
    
    assert max_running <= 2

async def test_rate_limit_respected():
    cue = runcue.Cue(":memory:")
    cue.service("api", rate_limit=(5, 1))  # 5 per second
    
    # Submit 10, verify only 5 run in first second
```

### Simulator (v0.4)

Service configuration:

```bash
runcue-sim --services mock_api:concurrent=5,rate=60/60 --count 100 --duration 30s
# Shows rate limiting in action
# Output includes: "Rate limited: 45 times"
```

---

## Phase 5: Multi-Service & Atomic Acquisition

**Goal**: Tasks can require multiple services; acquire atomically.

### Tasks

- [ ] Support multiple services per task type
- [ ] Implement ordered acquisition (alphabetical) to prevent deadlock
- [ ] All-or-nothing: release all if any unavailable
- [ ] Test deadlock scenarios don't occur

### Tests

```python
async def test_multi_service_acquisition():
    cue = runcue.Cue(":memory:")
    cue.service("api", max_concurrent=1)
    cue.service("storage", max_concurrent=1)
    
    @cue.task("both", services=["api", "storage"])
    async def both(work):
        await asyncio.sleep(0.1)
        return {}
    
    # Verify both acquired together

async def test_no_deadlock():
    # Submit many tasks needing [api, storage] and [storage, api]
    # Verify all complete (no deadlock)
```

### Simulator (v0.5)

Multi-service scenarios:

```bash
runcue-sim --config multi_service.yaml --duration 60s
# YAML defines tasks with multiple services
# Verifies no deadlocks under load
```

---

## Phase 6: Dependencies

**Goal**: Work units can depend on other work units.

### Tasks

- [ ] Add `depends_on` column to work_units (JSON array of IDs)
- [ ] Implement `dependency_satisfied` callback registration
- [ ] Default callback: check if dependent work is completed
- [ ] Block work until dependencies satisfied
- [ ] Fail dependents when prerequisite fails
- [ ] Detect circular dependencies at submit time

### Schema Addition

```sql
ALTER TABLE work_units ADD COLUMN depends_on TEXT;  -- JSON array
```

### Tests

```python
async def test_dependency_blocks_until_complete():
    cue = runcue.Cue(":memory:")
    # ... setup ...
    
    id_a = await cue.submit("task_a")
    id_b = await cue.submit("task_b", depends_on=[id_a])
    
    # B should not run until A completes

async def test_dependency_failure_cascades():
    # A fails → B marked failed with "prerequisite_failed"

async def test_circular_dependency_rejected():
    # Submit A depends on B, B depends on A → error at submit
```

### Simulator (v0.6)

Dependency patterns:

```bash
runcue-sim --pattern diamond --count 50 --duration 30s
# Creates diamond-shaped dependency graphs
# Verifies correct ordering
```

```bash
runcue-sim --pattern circular --expect-reject
# Verifies circular deps are rejected
```

---

## Phase 7: Retry Policy

**Goal**: Automatic retries with configurable backoff.

### Tasks

- [ ] Add retry fields to work_units (max_attempts, attempt, next_retry_at)
- [ ] Implement retry policy configuration on task types
- [ ] Implement backoff strategies: fixed, linear, exponential
- [ ] Add jitter to prevent thundering herd
- [ ] Distinguish transient vs permanent errors

### Tests

```python
async def test_retry_on_transient_error():
    fail_count = 0
    
    @cue.task("flaky", retry={"max_attempts": 3, "base_delay": 0.01})
    async def flaky(work):
        nonlocal fail_count
        fail_count += 1
        if fail_count < 3:
            raise TransientError("try again")
        return {"success": True}
    
    # Verify retries, eventually succeeds

async def test_exponential_backoff():
    # Verify delays increase exponentially

async def test_max_attempts_then_fail():
    # After max attempts, work is marked permanently failed
```

### Simulator (v0.7)

Error injection:

```bash
runcue-sim --error-rate 0.1 --retry-policy exponential --duration 60s
# 10% of work fails initially
# Shows retry behavior, eventual success rate
```

---

## Phase 8: Leasing

**Goal**: Crash recovery via lease expiration.

### Tasks

- [ ] Add lease fields to work_units (leased_by, lease_expires_at)
- [ ] Acquire lease before dispatch (lease_duration = timeout + buffer)
- [ ] Release lease on completion/failure
- [ ] On startup: reclaim expired leases
- [ ] Re-dispatch work with expired leases

### Tests

```python
async def test_lease_acquired_before_dispatch():
    # Work has lease while running

async def test_lease_released_on_completion():
    # Lease cleared after work completes

async def test_expired_lease_reclaimed_on_restart():
    cue = runcue.Cue("./test.db")
    # Start work, force "crash" (don't complete)
    # Verify lease_expires_at is set
    
    # Simulate restart
    cue2 = runcue.Cue("./test.db")
    await cue2.start()
    # Verify work is re-dispatched after lease expires
```

### Simulator (v0.8)

Crash simulation:

```bash
runcue-sim --scenario recovery --crash-after 10s --duration 30s
# Simulates crash mid-processing
# Verifies work resumes correctly
```

---

## Phase 9: Circuit Breaker

**Goal**: Stop sending to failing services.

### Tasks

- [ ] Track consecutive failures per service
- [ ] Implement circuit states: closed, open, half-open
- [ ] Block requests when circuit open
- [ ] Allow probe request in half-open state
- [ ] Close circuit on probe success

### Tests

```python
async def test_circuit_opens_after_failures():
    # Service fails 5 times → circuit opens

async def test_circuit_blocks_when_open():
    # Work for open-circuit service stays queued

async def test_circuit_closes_on_probe_success():
    # After cooldown, one request allowed
    # If succeeds, circuit closes
```

### Simulator (v0.9)

Circuit breaker scenarios:

```bash
runcue-sim --scenario cascade_failure --duration 60s
# One service fails repeatedly
# Shows circuit breaker activation
# Verifies work queues rather than fails
```

---

## Phase 10: Priority Callback

**Goal**: Dynamic priority based on application logic.

### Tasks

- [ ] Implement `@cue.priority` decorator
- [ ] Provide default priority (FIFO with starvation prevention)
- [ ] Pre-filter candidates before scoring
- [ ] Cache scores briefly for performance
- [ ] Provide `PriorityContext` with wait_time, queue_depth, etc.

### Tests

```python
async def test_higher_priority_runs_first():
    @cue.priority
    def prioritize(ctx):
        if ctx.work.params.get("urgent"):
            return 1.0
        return 0.5
    
    await cue.submit("task", params={"urgent": False})
    await cue.submit("task", params={"urgent": True})
    
    # Verify urgent runs first

async def test_starvation_prevention():
    # Old low-priority work eventually runs
```

### Simulator (v0.10)

Priority testing:

```bash
runcue-sim --scenario priority_test --duration 60s
# Mix of high/low priority work
# Verifies ordering, detects priority inversions
```

```bash
runcue-sim --scenario starvation --duration 120s
# Continuous high-priority stream
# Verifies low-priority eventually runs
```

---

## Phase 11: Events & Observability

**Goal**: Emit events for monitoring and UI.

### Tasks

- [ ] Implement event types (work_queued, started, completed, failed, etc.)
- [ ] Implement `cue.events()` async iterator
- [ ] Implement throttling (per-work-unit for progress, never for completions)
- [ ] Add basic metrics (counters, gauges)

### Tests

```python
async def test_events_emitted():
    events = []
    
    async def collect():
        async for event in cue.events():
            events.append(event)
            if event.type == "completed":
                break
    
    asyncio.create_task(collect())
    await cue.submit("task")
    
    await asyncio.sleep(0.2)
    
    event_types = [e.type for e in events]
    assert "work_queued" in event_types
    assert "work_started" in event_types
    assert "work_completed" in event_types
```

### Simulator (v0.11)

Event display:

```bash
runcue-sim --count 50 --show-events
# Streams events as they occur
```

TUI becomes viable here — shows queue state, events, service status.

---

## Phase 12: Cancellation

**Goal**: Cancel work explicitly.

### Tasks

- [ ] Implement `cue.cancel(work_id)`
- [ ] Implement `cascade=True` option
- [ ] Signal running handlers via `work.cancelled`
- [ ] Mark queued dependents appropriately

### Tests

```python
async def test_cancel_queued_work():
    work_id = await cue.submit("task")
    await cue.cancel(work_id)
    
    work = await cue.get(work_id)
    assert work.state == "cancelled"

async def test_cancel_cascade():
    id_a = await cue.submit("task")
    id_b = await cue.submit("task", depends_on=[id_a])
    
    await cue.cancel(id_a, cascade=True)
    
    work_b = await cue.get(id_b)
    assert work_b.state == "cancelled"
```

### Simulator (v0.12)

Interactive cancellation:

```bash
runcue-sim --tui
# Press K to cancel running work
# Observe cascade behavior
```

---

## Phase 13: Process Pool & Subprocess Executors

**Goal**: Support CPU-bound and shell command work.

### Tasks

- [ ] Implement process pool executor
- [ ] Implement subprocess executor (handler returns command)
- [ ] Capture stdout/stderr for subprocess
- [ ] Handle timeouts for both executor types
- [ ] Ensure cross-platform compatibility (spawn on Windows)

### Tests

```python
async def test_process_pool_executor():
    @cue.task("cpu_work", executor="process_pool")
    def heavy_compute(work):
        return {"result": sum(range(1000000))}
    
    # Verify runs in separate process, returns result

async def test_subprocess_executor():
    @cue.task("shell", executor="subprocess")
    def run_command(work):
        return ["echo", "hello"]
    
    # Verify command executed, stdout captured
```

### Simulator (v0.13)

Executor mix:

```bash
runcue-sim --executors async:0.6,process:0.3,subprocess:0.1 --duration 60s
# Mix of executor types
# Verifies all work correctly
```

---

## Phase 14: Backpressure

**Goal**: Prevent unbounded queue growth.

### Tasks

- [ ] Implement `max_queued` configuration
- [ ] Implement `on_backpressure` callback
- [ ] Implement reject policies: `reject`, `drop_oldest`, `block`
- [ ] Emit `backpressure_active` events

### Tests

```python
async def test_backpressure_rejects():
    cue = runcue.Cue(":memory:", max_queued=10)
    
    for i in range(15):
        try:
            await cue.submit("task")
        except BackpressureError:
            assert i >= 10
```

### Simulator (v0.14)

Burst testing:

```bash
runcue-sim --scenario burst --max-queued 1000 --duration 60s
# Sudden spike to 200% capacity
# Shows backpressure handling
```

---

## Phase 15: Full Integration & Polish

**Goal**: Complete API, documentation, edge cases.

### Tasks

- [ ] Dependency timeout handling
- [ ] Service log retention configuration
- [ ] Complete error messages and validation
- [ ] API documentation
- [ ] Performance optimization pass
- [ ] Example applications

### Simulator (Final)

Full TUI with all features:

```bash
runcue-sim --tui --scenario steady_load --duration 5m
```

All interactive controls working, metrics tracked, scenarios available.

---

## Dependency Graph

```text
Phase 0: Setup
    │
    ▼
Phase 1: Data Models ──────────────────────────────┐
    │                                              │
    ▼                                              │
Phase 2: Queue Ops                                 │
    │                                              │
    ▼                                              │
Phase 3: Async Executor ◄──────────────────────────┤
    │                                              │
    ├──────────────────┬───────────────┐          │
    ▼                  ▼               ▼          │
Phase 4: Rate      Phase 6:       Phase 7:        │
Limiting           Dependencies   Retry           │
    │                  │               │          │
    ▼                  │               │          │
Phase 5: Multi-        │               │          │
Service                │               │          │
    │                  │               │          │
    └──────────────────┴───────────────┘          │
                       │                          │
                       ▼                          │
               Phase 8: Leasing ◄─────────────────┘
                       │
                       ▼
               Phase 9: Circuit Breaker
                       │
           ┌───────────┴───────────┐
           ▼                       ▼
    Phase 10: Priority      Phase 11: Events
           │                       │
           └───────────┬───────────┘
                       ▼
               Phase 12: Cancellation
                       │
                       ▼
               Phase 13: Executors
                       │
                       ▼
               Phase 14: Backpressure
                       │
                       ▼
               Phase 15: Polish
```

---

## Milestones

| Milestone | Phases | Capability |
|-----------|--------|------------|
| **M1: Proof of Concept** | 0-3 | Submit work, see it execute |
| **M2: Rate-Limited** | 4-5 | Respect service limits |
| **M3: Dependencies** | 6-7 | Chains of work, retries |
| **M4: Production-Ready** | 8-11 | Crash recovery, observability |
| **M5: Complete** | 12-15 | All features, polished |

---

## Notes

- Each phase should take 1-3 days of focused work
- Tests are written first, then implementation
- Simulator grows incrementally — don't over-engineer early versions
- Keep the core loop simple; complexity lives in the broker/scheduler
- SQLite WAL mode should be enabled from Phase 1


