# runcue

**Status**: Design Specification (Dec 2025)

> **runcue** provides the cues and coordination to run tasks many tasks at once — monitoring dependencies, rate limits, retries, and progress, so you only need to define the workloads.

A Python library for coordinating work across rate-limited services, remote hosts, and external processes. Acts as an **traffic controller** — it doesn't run the heavy computation itself, but dispatches work to external systems (AI APIs, SSH hosts, batch processors) and tracks completion.

## Quick Example

```python
import runcue

# Define services with rate limits
cue = runcue.Cue("./queue.db")
cue.service("openai", rate_limit=(60, 60), max_concurrent=5)  # 60 req/min
cue.service("s3", rate_limit=(100, 60), max_concurrent=10)

# Register task types
@cue.task("extract", services=["openai"], retry=3)
async def extract_text(work):
    response = await openai.chat(model="gpt-4", ...)
    return {"text": response.content}

@cue.task("upload", services=["s3"])
async def upload_result(work):
    await s3.put_object(work.params["key"], work.params["data"])
    return {"url": f"s3://{bucket}/{work.params['key']}"}

# Set priority (optional - defaults to FIFO with starvation prevention)
@cue.priority
def prioritize(ctx):
    if ctx.work.params.get("urgent"):
        return 1.0
    return 0.5 + min(ctx.wait_time / 3600, 0.4)  # Boost older work

# Submit work with dependencies
async def process_document(doc_path):
    extract_id = await cue.submit("extract", target=doc_path)
    upload_id = await cue.submit("upload", depends_on=[extract_id])
    return upload_id

# Run and monitor
async def main():
    cue.start()  # Non-blocking, starts background loop
    
    work_id = await process_document("/path/to/doc.pdf")
    
    async for event in cue.events():
        if event.work_id == work_id and event.type == "completed":
            print(f"Done: {event.result}")
            break
    
    await cue.stop()  # Graceful shutdown
```

For shell commands and CPU-bound work:

```python
# Shell/SSH tasks
@cue.task("deploy", services=["ssh_prod"], executor="subprocess")
def deploy_config(work):
    return ["ssh", work.params["host"], "sudo systemctl reload nginx"]

# CPU-bound tasks (runs in process pool)
@cue.task("parse_pdf", executor="process_pool")
def parse_pdf(work):
    return extract_pages(work.target)  # Heavy computation
```

---

## Problem Statement

Applications that coordinate external work face common challenges:

1. **Rate limits** — External APIs have request limits that must be respected
2. **Resource contention** — Limited SSH connections, API tokens, or processing slots
3. **Dependencies** — Work units depend on outputs from previous work
4. **Progress visibility** — Users need to see what's happening
5. **Failure recovery** — Work should resume after crashes without data loss

This library provides a resource-aware dispatcher. Applications define work units and their requirements; the library handles scheduling, rate limiting, and coordination. The actual work runs elsewhere — in AI APIs, on remote hosts, or in separate processes.

---

## Architecture

```text
┌─────────────────────────────────────────────────────────┐
│  Application                                            │
│                                                         │
│  ┌───────────────┐    ┌───────────────┐                │
│  │ Web UI / CLI  │───▶│ Work Submitter│                │
│  └───────────────┘    └───────┬───────┘                │
│                               │                         │
│  ┌────────────────────────────┼────────────────────┐   │
│  │                            ▼                    │   │
│  │  ┌────────────────────────────────────────────┐ │   │
│  │  │              Orchestrator                  │ │   │
│  │  │                                            │ │   │
│  │  │  Work Queue ◀───▶ Resource Broker          │ │   │
│  │  │       │                 │                  │ │   │
│  │  │       ▼                 ▼                  │ │   │
│  │  │  Priority       Service Limits             │ │   │
│  │  │  Callback       Rate History               │ │   │
│  │  │       │                                    │ │   │
│  │  │       ▼                                    │ │   │
│  │  │  Dependency ◀─── Application Callback      │ │   │
│  │  │  Checker         (artifact exists?)        │ │   │
│  │  └────────────────────────────────────────────┘ │   │
│  │                            │                    │   │
│  │            Progress Reporter ───────────────────┼───┼──▶ SSE/WebSocket
│  │                                                 │   │
│  └─────────────────────────────────────────────────┘   │
│                               │                         │
│            ┌──────────────────┼──────────────────┐     │
│            ▼                  ▼                  ▼     │
│     ┌───────────┐      ┌───────────┐      ┌─────────┐ │
│     │  AI APIs  │      │ SSH Hosts │      │ Process │ │
│     │ (OpenAI,  │      │ (deploy,  │      │  Pool   │ │
│     │ Anthropic)│      │  verify)  │      │ (local) │ │
│     └───────────┘      └───────────┘      └─────────┘ │
│                                                        │
│                        ┌─────────────┐                │
│                        │   SQLite    │                │
│                        │ (queue +    │                │
│                        │  rate log)  │                │
│                        └─────────────┘                │
└────────────────────────────────────────────────────────┘
```

**Design principle**: The orchestrator is a coordinator, not an executor. Real work happens in external systems (AI APIs, remote hosts) or separate processes. The orchestrator decides *what* can run and *when*, tracks service usage, and reports progress. This is why a single async process is sufficient — the orchestrator isn't doing heavy computation, just dispatching and bookkeeping. With this approach, we simplify the architecture and reduce the need for things like redis, separate containers, queue services, etc. - just sqlite and this library are sufficient.

---

## Core Concepts

### Work Unit

A work unit is a request to perform work. It declares what it needs; the orchestrator decides when to dispatch it.

| Field | Description |
|-------|-------------|
| `id` | Unique identifier |
| `task_type` | What kind of work (references a registered task) |
| `target` | What to process (file path, record ID, URL, etc.) |
| `params` | Task-specific parameters |
| `depends_on` | IDs of work units that must complete first |
| `timeout` | Maximum execution time (seconds) |
| `dependency_timeout` | How long to wait for dependencies before failing |
| `idempotency_key` | Optional key for duplicate detection |

Work units don't specify priority directly. Priority is determined dynamically by the orchestrator using application-provided logic.

**Service requirements**: Work units inherit service requirements from their task type. If different service combinations are needed, define separate task types — this keeps service usage predictable and simplifies scheduling.

**Idempotency**: When `idempotency_key` is provided, the library enforces uniqueness:

| Existing Work State | Behavior |
|---------------------|----------|
| Pending or running | Return existing work ID |
| Completed | Return existing work ID |
| Failed | Configurable: reject or allow resubmit |

Without an explicit key, applications can enforce uniqueness on `(task_type, target, params_hash)` in their own logic.

### Service

A service represents a rate-limited or capacity-limited external resource:

- **APIs** — OpenAI, Anthropic, cloud services
- **Remote hosts** — SSH connections to servers
- **Local pools** — Process pool slots for CPU-bound work

Each service defines its constraints:

| Constraint | Description | Example |
|------------|-------------|---------|
| `max_concurrent` | Simultaneous requests allowed | `5` |
| `rate_limit` | Requests per time window | `60` |
| `rate_window` | Time window in seconds | `60` (= 60 per minute) |
| `circuit_threshold` | Failures before blocking | `5` |
| `circuit_timeout` | Seconds before retry | `300` |

**Rate limit examples**:
- `rate_limit: 60, rate_window: 60` → 60 requests per minute
- `rate_limit: 1000, rate_window: 3600` → 1000 requests per hour
- `rate_limit: 10, rate_window: 1` → 10 requests per second

### Task Type

A task type defines how to dispatch a category of work:

| Field | Description |
|-------|-------------|
| `name` | Identifier (e.g., "extract", "deploy", "analyze") |
| `services` | Which services this task type always requires |
| `executor` | How to run: `async`, `process_pool`, or `subprocess` |
| `handler` | The function that dispatches the work |
| `retry` | Retry policy for transient failures |

### Retry Policy

Tasks can define retry behavior for transient failures:

| Field | Description | Default |
|-------|-------------|---------|
| `max_attempts` | Total attempts before permanent failure | 3 |
| `backoff` | Backoff strategy: `fixed`, `linear`, `exponential` | `exponential` |
| `base_delay` | Initial delay between retries (seconds) | 1.0 |
| `max_delay` | Maximum delay between retries (seconds) | 300 |
| `jitter` | Add randomness to prevent thundering herd | true |
| `retry_on` | Error types that trigger retry | transient errors |

The orchestrator tracks retry state per work unit: `attempt_count`, `next_retry_at`, `last_error`.

### Dependencies and Artifacts

The orchestrator needs to know when dependencies are satisfied, but **does not manage artifacts itself**. Instead, the application provides a **dependency checker callback**:

```text
Application provides:
  dependency_satisfied(work_unit_id: str) -> bool

Orchestrator calls this to check:
  - Has this work unit's output been produced?
  - Is the output still valid (not stale)?
```

This keeps artifact storage, format, and staleness logic entirely within the application. The orchestrator only knows: "can this dependent work unit proceed?"

**Why this separation?**

- Applications have different artifact needs (files, database records, external storage)
- Staleness logic varies by application (hash-based, timestamp-based, always-fresh)
- The orchestrator shouldn't dictate storage patterns

**Result passing pattern**: runcue does not pass results between tasks. Applications use conventions or params to locate outputs:

```text
# Producer stores output at predictable path
split_pdf handler → writes to outputs/blocks/{page:03d}/source.png

# Consumer receives location info via params
extract_text params: {"page_num": 5, "source_work_id": "split_123"}
extract_text handler → reads from outputs/blocks/005/source.png

# dependency_satisfied confirms file exists before dispatch
```

The `depends_on` relationship ensures ordering; the application owns the data flow.

---

## Scheduling

### Dynamic Priority

Rather than assigning fixed priority numbers, the orchestrator uses a **priority callback** provided by the application. When deciding what to dispatch next, the orchestrator:

1. Gathers all eligible work units (dependencies satisfied, services available)
2. Calls the application's priority function for each
3. Dispatches the highest-scoring work unit

The priority callback receives context about the work unit and system state:

```text
PriorityContext:
  work_unit: the candidate work unit
  queue_depth: how many items are waiting
  service_pressure: map of service → current load percentage
  wait_time: how long this unit has been queued
```

The callback returns a float from 0.0 (lowest) to 1.0 (highest).

**Example priorities an application might implement**:

| Use Case | Priority Logic |
|----------|----------------|
| User viewing a page | Return 1.0 for work affecting the current page |
| Interactive request | Return 0.9 for user-initiated work |
| Batch processing | Return 0.3 for background work |
| Starvation prevention | Boost priority based on `wait_time` |

This approach lets applications implement context-aware scheduling without the orchestrator needing to understand application semantics.

### Dependency Resolution

Work units can declare dependencies on other work units. The orchestrator:

1. Calls the application's `dependency_satisfied` callback for each dependency
2. Holds dependent work until all dependencies return `true`
3. Fails dependents immediately if a prerequisite fails or is cancelled
4. Fails work units whose dependencies don't resolve within `dependency_timeout`

```text
Work unit "deploy_config" depends_on ["validate_config"]

Orchestrator checks:
  dependency_satisfied("validate_config") → true?
    yes → "deploy_config" is eligible to run
    no  → "deploy_config" stays queued (until timeout)
```

**Dependency failure modes**:

| Condition | What Happens |
|-----------|--------------|
| Dependency fails | Dependent fails immediately, `error="prerequisite_failed"` |
| Dependency cancelled | Dependent fails immediately, `error="prerequisite_cancelled"` |
| Dependency times out | Dependency fails, then dependent fails as above |
| `dependency_timeout` exceeded | Dependent fails, `error="dependency_timeout"` |

The `dependency_timeout` covers waiting scenarios: dependency stuck in queue, in retry backoff, or blocked by rate limits. It's a "give up waiting" threshold separate from execution timeout.

### Dynamic Work Submission

Task handlers should be **pure** — they perform their work and return results, with no knowledge of orchestration. Dynamic follow-up work is submitted by the application in response to events:

```text
# WRONG: submitting inside handler
@cue.task("split_pdf")
async def split_pdf(work):
    pages = do_split(work.target)
    for page in pages:
        await cue.submit("extract", ...)  # Don't do this
    return {"pages": len(pages)}

# RIGHT: submitting in event handler
async for event in cue.events():
    if event.type == "completed" and event.task_type == "split_pdf":
        for page in range(1, event.result["pages"] + 1):
            await cue.submit("extract_text",
                params={"page_num": page},
                depends_on=[event.work_id])
```

This separation keeps handlers testable and lets the application control workflow logic.

### Service Scheduling

When multiple work units are eligible and competing for the same service:

1. Calculate priority scores for all candidates
2. Sort by score (highest first)
3. Grant service access to highest-priority work that fits within limits
4. Re-evaluate when any work completes or new work arrives

---

## Resource Management

### Rate Limiting

The resource broker maintains a log of service usage in SQLite. When a work unit requests a service:

1. **Check concurrent limit** — Count active uses of this service
2. **Check rate limit** — Count requests in the current time window
3. **Check circuit breaker** — Has this service failed too many times?

If all checks pass, grant access. Otherwise, the work unit waits.

**Rate calculation**:

```text
Service: anthropic (rate_limit: 30, rate_window: 60)

Query: How many requests in the last 60 seconds?
  SELECT COUNT(*) FROM service_log
  WHERE service = 'anthropic'
    AND started_at > now() - 60

If count >= 30: deny access (rate limited)
If count < 30: grant access, log new request
```

Rate limits are tracked in SQLite, so they survive restarts. After a crash, the orchestrator won't accidentally exceed limits because it can see recent history.

**Service log retention**: Old entries are pruned automatically. Configurable retention (default: 24 hours):

```python
cue = runcue.Cue("./db", service_log_retention=86400)  # 24h
```

Pruning runs periodically in the background (e.g., hourly).

### Multi-Service Acquisition

When a task requires multiple services (e.g., both `openai` and `s3`), the broker acquires them **atomically** using ordered locking to prevent deadlock:

```text
Work needs [s3, openai] → broker acquires in sorted order: [openai, s3]

If any acquisition fails:
  1. Release all already-acquired services
  2. Re-queue work unit for later evaluation
```

No partial holds means no deadlock. All-or-nothing acquisition keeps scheduling predictable.

### Circuit Breaker

After consecutive failures to a service, the circuit "opens" and blocks requests for a cooldown period:

| State | Behavior |
|-------|----------|
| **Closed** | Normal operation, requests flow through |
| **Open** | Blocking all requests, waiting for cooldown |
| **Half-open** | Cooldown expired, allowing one probe request |

**Probe behavior**: The probe is a real work unit — the highest-priority one waiting for that service. It counts against rate limits. If it succeeds, the circuit closes and normal flow resumes. If it fails, the circuit reopens. No synthetic test requests are used.

### Leasing for Atomic Dispatch

To prevent duplicate dispatch after crashes, work units use a **lease** model:

1. When dispatching, orchestrator acquires a lease (sets `leased_by`, `lease_expires_at`)
2. Work unit is only considered "running" while lease is valid
3. If orchestrator crashes, lease expires and work can be re-dispatched
4. On completion, lease is released and work marked complete

This prevents the scenario where work is dispatched, orchestrator crashes before recording completion, and work is dispatched again on restart.

**Lease duration**: Set to work unit `timeout` plus a buffer (default +60 seconds). For a task with `timeout=1800` (30 minutes), the lease expires at 1860 seconds.

No heartbeat mechanism initially — handlers don't need to report liveness. Future enhancement: optional heartbeat for very long-running tasks.

### Resource Lifecycle

```text
Work unit eligible (dependencies satisfied)
    │
    ▼
Request required services from broker
    │
    ├─► All available → Acquire lease, dispatch work
    │                         │
    │                         ├─► Success → Release lease, mark complete
    │                         │
    │                         └─► Failure → Release lease, maybe retry
    │                                        (may open circuit)
    │
    └─► Some unavailable → Stay queued, re-evaluate later
```

---

## Execution Model

### Executor Types

| Type | Use When | What Happens |
|------|----------|--------------|
| `async` | API calls, network I/O | Handler runs as asyncio coroutine |
| `process_pool` | CPU-bound local work | Handler runs in separate process |
| `subprocess` | External commands | Runs SSH, CLI tools via subprocess |

Even `process_pool` and `subprocess` work is "external" from the orchestrator's perspective — it runs in a separate process or on a remote host. The orchestrator's event loop remains free for coordination.

### Handler Return Values

Each executor type has different expectations for what handlers return:

| Executor | Handler Returns | Result Stored |
|----------|-----------------|---------------|
| `async` | Result dict | The dict directly |
| `process_pool` | Result dict | The dict directly |
| `subprocess` | Command list `[str]` | `{stdout, stderr, exit_code}` |

For `subprocess`, the handler *builds* the command; runcue *runs* it and captures output:

```python
@cue.task("deploy", services=["ssh_prod"], executor="subprocess")
def deploy_config(work):
    # Handler returns the command to execute
    return ["ssh", work.params["host"], "sudo systemctl reload nginx"]

# runcue runs the command and stores:
# {"stdout": "...", "stderr": "...", "exit_code": 0}
```

### Process Pool Communication

For `process_pool` executors, the orchestrator communicates with workers via:

1. **Task dispatch**: Serialize work unit params, send to worker process
2. **Result collection**: Worker returns JSON result via stdout or queue
3. **Health monitoring**: Heartbeat timeout detects crashed workers
4. **Cancellation**: Signal worker process on timeout or cancel

Workers should be stateless — any state needed for the task is passed in params or read from the filesystem.

### Timeout Handling

Every work unit has a timeout. If exceeded:

1. Cancel the work (signal the process/subprocess)
2. Mark as failed with timeout error
3. Release any held services and leases
4. Retry if policy allows, otherwise permanent failure

### Failure Handling

| Failure Type | Default Behavior |
|--------------|------------------|
| Timeout | Retry based on policy |
| Transient (network, rate limit) | Retry with exponential backoff |
| Permanent (invalid input, auth) | Mark failed, don't retry |

Work units that exhaust all retry attempts can optionally move to a **dead letter queue** for manual inspection.

### Cancellation

Applications can cancel work explicitly:

```python
await cue.cancel(work_id)                # Cancel single work unit
await cue.cancel(work_id, cascade=True)  # Cancel + all dependents
```

**Cancellation behavior**:

| Work State | What Happens |
|------------|--------------|
| Queued | Marked `cancelled` immediately |
| Running | Signaled to stop; handler can check `work.cancelled` |
| Completed/Failed | No change (already finished) |

**Dependent handling**:

- Without `cascade`: dependents fail with `error="prerequisite_cancelled"`
- With `cascade`: dependents are also marked `cancelled`

Cancelled work emits `work_cancelled` event.

---

## Backpressure

To prevent unbounded queue growth under load:

| Mechanism | Description |
|-----------|-------------|
| `max_queued` | Maximum pending work units (reject new submissions when full) |
| `on_backpressure` | Callback when queue approaches limit |
| `reject_policy` | What to do when full: `reject`, `drop_oldest`, `block` |

When backpressure is active, the orchestrator emits `backpressure_active` events so the application can inform users or throttle submissions.

---

## State Persistence

### What's Stored

The orchestrator maintains a SQLite database with:

| Table | Purpose |
|-------|---------|
| `work_units` | Queue with state, timing, retry info, lease |
| `services` | Service definitions and constraints |
| `service_log` | Usage history for rate limiting |
| `circuit_state` | Circuit breaker state per service |

**Note**: Artifacts are NOT stored by the orchestrator. The application manages its own artifact storage and provides the `dependency_satisfied` callback.

### Work Unit State

| Field | Purpose |
|-------|---------|
| `state` | queued, running, completed, failed, cancelled |
| `attempt` | Current attempt number |
| `next_retry_at` | When to retry (if failed and retryable) |
| `last_error` | Most recent error message |
| `leased_by` | Which dispatcher holds the lease |
| `lease_expires_at` | When lease expires |

### Recovery

On startup, the orchestrator:

1. Reclaims expired leases (work units where `lease_expires_at < now`)
2. Cleans up stale service locks from `service_log`
3. Rebuilds the in-memory queue from persisted state
4. Resumes work units that were queued or pending retry

Work that was in progress when the crash occurred will be re-dispatched (lease expired). Handlers should be idempotent or use atomic operations (write to temp file, then rename).

---

## Observability

### Metrics

The orchestrator exposes metrics for monitoring:

| Metric | Type | Description |
|--------|------|-------------|
| `work_units_queued` | Gauge | Current queue depth |
| `work_units_completed_total` | Counter | Total completions |
| `work_units_failed_total` | Counter | Total failures |
| `service_requests_total` | Counter | Requests per service |
| `service_rate_limited_total` | Counter | Rate limit hits |
| `circuit_breaker_open` | Gauge | Open circuits by service |
| `callback_duration_seconds` | Histogram | Priority/dependency callback latency |

### Structured Logging

Events include structured context for debugging:

```text
work_started: work_unit_id=abc123, task_type=extract, attempt=2
work_failed: work_unit_id=abc123, error=timeout, will_retry=true
service_rate_limited: service=openai, current_rate=61, limit=60
circuit_opened: service=ssh_prod_1, failures=5
```

### Event Stream

The orchestrator emits events that applications can forward to UIs:

| Event | When |
|-------|------|
| `work_queued` | New work unit added |
| `work_started` | Work unit began executing |
| `work_completed` | Work unit finished successfully |
| `work_failed` | Work unit failed (includes retry info) |
| `work_retrying` | Work unit scheduled for retry |
| `work_cancelled` | Work unit was cancelled |
| `queue_status` | Periodic summary of queue state |
| `service_status` | Service availability changed |
| `backpressure_active` | Queue approaching limit |

**Event throttling**:

| Event Type | Throttling |
|------------|------------|
| Progress updates | Throttled to 1/sec per work unit |
| `work_completed`, `work_failed`, `work_retrying`, `work_cancelled` | Never throttled (always delivered) |
| `queue_status` | Periodic (configurable interval) |

Important events are never dropped. Throttling only affects high-frequency progress updates.

### Integration Options

| UI Technology | Integration |
|---------------|-------------|
| Server-Sent Events | Subscribe to event stream, push to client |
| WebSocket | Forward events through socket |
| HTMX | Return HTML fragments, use `hx-trigger="sse:event_name"` |
| Polling | Expose `/status` endpoint for periodic fetch |

---

## Callback Contracts

Application-provided callbacks must follow these contracts:

### Priority Callback

```text
priority(ctx: PriorityContext) -> float

Requirements:
  - Must return within 100ms (timeout enforced)
  - Must be non-blocking (no I/O, no locks)
  - Must be deterministic for same input
  - Returns 0.0 to 1.0

If callback times out or raises:
  - Work unit receives default priority (0.5)
  - Error logged, callback_errors metric incremented
```

**Scaling**: The priority callback is not invoked for all queued work units. The orchestrator pre-filters before scoring:

1. Filter to work units whose dependencies are satisfied
2. Filter to work units whose services have capacity
3. Score only the filtered set

With 10,000 queued items but only 5 `openai` slots available, we might score only ~100 candidates waiting for `openai`, not all 10,000. Scores are cached briefly (e.g., 100ms) if the callback is expensive.

### Dependency Callback

```text
dependency_satisfied(work_unit_id: str) -> bool

Requirements:
  - Must return within 100ms (timeout enforced)
  - Should be fast (database lookup or file existence check)
  - Must be idempotent (same answer for same state)

If callback times out or raises:
  - Dependency treated as not satisfied
  - Work unit stays queued
  - Error logged
```

### Default Implementations

The library provides default callbacks for common patterns:

| Default | Behavior |
|---------|----------|
| `priority_by_wait_time` | Older work gets higher priority (starvation prevention) |
| `priority_constant(0.5)` | All work equal priority |
| `dependency_check_file(pattern)` | Check if output file exists |
| `dependency_check_db(table, column)` | Query database for completion record |

---

## Application Integration

### What the Application Provides

| Callback | Purpose |
|----------|---------|
| `priority(ctx) → float` | Score work units 0.0-1.0 for scheduling |
| `dependency_satisfied(id) → bool` | Check if a work unit's output exists |
| Task handlers | Functions that dispatch the actual work |

### Setup Steps

**1. Initialize the orchestrator**

Provide database path and configuration:
- Process pool size
- Event throttle rate
- Backpressure limits
- Lease timeout

**2. Register services**

For each external resource, specify:
- `max_concurrent` — simultaneous request limit
- `rate_limit` and `rate_window` — X requests per Y seconds
- `circuit_threshold` and `circuit_timeout` — failure handling

**3. Register task types**

For each kind of work, specify:
- Which services it requires
- Which executor to use (`async`, `process_pool`, `subprocess`)
- The handler function that dispatches the work
- Retry policy for transient failures

**4. Provide callbacks**

- Priority callback: receives work unit + context, returns 0.0-1.0
- Dependency callback: receives work unit ID, returns true if output exists
- Or use provided defaults

**5. Wire up your web framework**

- Route to submit work units
- Route to query status
- SSE/WebSocket endpoint for progress events
- Startup/shutdown hooks for orchestrator lifecycle

### Orchestrator Lifecycle

```python
cue.start()        # Non-blocking, starts orchestrator loop as background asyncio task
await cue.submit(...)  # Works immediately because orchestrator is running
# ...
await cue.stop()   # Graceful shutdown: finishes running work, persists queue
```

- `start()` returns immediately; the scheduling loop runs in background tasks
- `stop()` is async; it waits for currently running work to complete before returning

### Minimal Integration Flow

```text
Application startup:
  1. Create orchestrator with db_path
  2. Register services with rate limits
  3. Register task types with handlers
  4. Set priority callback (or use default)
  5. Set dependency callback (or use default)
  6. Call cue.start() — returns immediately

Handling a request:
  1. Create work unit describing what to do
  2. Submit to orchestrator (returns ID or backpressure error)
  3. Return work unit ID to client

Orchestrator loop (automatic):
  1. Find eligible work (dependencies satisfied)
  2. Score by priority callback
  3. Check service availability
  4. Acquire lease, dispatch highest-priority eligible work
  5. On completion: release lease, emit events
  6. On failure: release lease, schedule retry or mark failed

Application shutdown:
  1. await cue.stop() — waits for running work, then persists queue
```

---

## Example: Document Processing Application

An application that converts uploaded documents to searchable text with AI analysis.

### Document App: Services

| Service | Rate Limit | Concurrency | Purpose |
|---------|------------|-------------|---------|
| `openai` | 60/min | 5 | Text extraction, analysis |
| `s3` | 100/min | 10 | File storage |
| `process_pool` | — | 4 | PDF parsing, image processing |

### Document App: Task Types

| Task | Services | Executor | Retry |
|------|----------|----------|-------|
| `split_pdf` | `process_pool` | process_pool | 2 attempts |
| `extract_text` | `openai` | async | 3 attempts, exponential |
| `analyze` | `openai` | async | 3 attempts, exponential |
| `upload_result` | `s3` | async | 3 attempts |

### Document App: Callbacks

**Priority**:

```text
def priority(ctx):
    work = ctx.work_unit
    
    # User is viewing this document - highest priority
    if work.params.get("document_id") == current_user_document:
        if work.params.get("page") == current_user_page:
            return 1.0
        return 0.85
    
    # Interactive upload - high priority
    if work.params.get("interactive"):
        return 0.7
    
    # Starvation prevention
    wait_hours = ctx.wait_time / 3600
    return min(0.3 + (wait_hours * 0.1), 0.6)
```

**Dependency checker**:

```text
def dependency_satisfied(work_unit_id):
    # Application checks its own artifact storage
    artifact = db.query(Artifact).filter_by(work_unit_id=work_unit_id).first()
    return artifact is not None and not artifact.is_stale()
```

### Document App: Work Flow

```text
User uploads document
    │
    ▼
Submit: split_pdf(document_path)
    │
    ▼
split_pdf completes → application stores page images
    │
    ▼
For each page: submit extract_text(page_image)
    depends_on: [split_pdf_id]
    │
    ▼
All pages complete → application stores text
    │
    ▼
Submit: analyze(document_id)
    depends_on: [all extract_text IDs]
    │
    ▼
Submit: upload_result(analysis)
    depends_on: [analyze_id]
```

---

## Example: Server Management Tool

An application that manages infrastructure by generating configurations with AI and deploying via SSH.

### Server Tool: Services

| Service | Rate Limit | Concurrency | Purpose |
|---------|------------|-------------|---------|
| `anthropic` | 30/min | 3 | Configuration generation |
| `ssh_prod_1` | — | 1 | Production server 1 |
| `ssh_prod_2` | — | 1 | Production server 2 |
| `ssh_staging` | — | 2 | Staging servers |

### Server Tool: Task Types

| Task | Services | Executor | Retry |
|------|----------|----------|-------|
| `scan_inventory` | `ssh_*` | subprocess | 2 attempts |
| `generate_config` | `anthropic` | async | 3 attempts |
| `validate_config` | `process_pool` | process_pool | 1 attempt |
| `deploy_config` | `ssh_*` | subprocess | 2 attempts |
| `verify_deployment` | `ssh_*` | subprocess | 3 attempts |

### Server Tool: Callbacks

**Priority**:

```text
def priority(ctx):
    work = ctx.work_unit
    
    # Urgent deployments (incident response)
    if work.params.get("urgent"):
        return 1.0
    
    # Verification after deployment - needs to run soon
    if work.task_type == "verify_deployment":
        return 0.9
    
    # Active deployment - keep momentum
    if work.task_type == "deploy_config":
        return 0.8
    
    # Config generation - moderate priority
    if work.task_type in ["generate_config", "validate_config"]:
        return 0.5
    
    # Inventory scans - background
    return 0.2
```

**Dependency checker**:

```text
def dependency_satisfied(work_unit_id):
    # Check if output file exists for this work unit
    work = get_work_unit(work_unit_id)
    output_path = get_output_path(work)
    return output_path.exists()
```

### Server Tool: Work Flow

```text
User requests new configuration for service X
    │
    ▼
Submit: scan_inventory(target_hosts)
    │
    ▼
Inventory complete → stored in application DB
    │
    ▼
Submit: generate_config(requirements)
    depends_on: [scan_inventory_id]
    │
    ▼
LLM generates configuration → stored as file
    │
    ▼
Submit: validate_config(config_path)
    depends_on: [generate_config_id]
    │
    ▼
Validation passes
    │
    ▼
For each host: submit deploy_config(host, config)
    depends_on: [validate_config_id]
    │
    ▼
Each deployment completes
    │
    ▼
For each host: submit verify_deployment(host)
    depends_on: [deploy_config_id for this host]
```

### SSH Service Constraints

- `ssh_prod_1` and `ssh_prod_2`: `max_concurrent: 1` — one operation per production host
- `ssh_staging`: `max_concurrent: 2` — staging can handle parallel operations
- Each SSH service has independent circuit breaker — unreachable hosts don't block others

---

## Design Decisions

### Why "air traffic controller" (not job executor)?

This library coordinates work that runs elsewhere:

- **AI APIs** — Actual inference runs on OpenAI/Anthropic servers
- **SSH commands** — Actual work runs on remote hosts
- **Process pool** — Even local CPU work runs in separate processes

The orchestrator's job is scheduling and coordination, not computation. This is why a single async process is sufficient — we're not doing heavy lifting, just dispatching and tracking.

### Why callback-based priority (not numeric)?

Numeric priorities require the application to assign numbers at submission time, before knowing the full context. Callback-based priority:

- Adapts to current system state (queue depth, service pressure)
- Responds to application context (which document is the user viewing?)
- Implements starvation prevention naturally (factor in wait time)
- Doesn't require the orchestrator to understand application semantics

### Why application-owned artifacts?

The orchestrator only needs to know "can this work proceed?" — not where artifacts are stored, what format they use, or how staleness is determined. Applications provide a simple `dependency_satisfied(id) → bool` callback.

This avoids:
- Forcing a particular storage pattern
- Duplicating artifact metadata in multiple places
- Coupling orchestration to artifact implementation

### Why single-process (not distributed)?

This library coordinates work that already runs on distributed systems (AI APIs, remote hosts). The orchestrator itself doesn't need to be distributed because:

- It's not doing computation — just dispatching and bookkeeping
- SQLite handles persistence efficiently for single-writer workloads
- A single async process can dispatch thousands of work units per hour

**Scaling approach**: Run independent orchestrator instances for different workloads (e.g., one per tenant, one per job type). This is workload isolation, not horizontal scaling of a single queue.

### Why SQLite (not Redis/PostgreSQL)?

- **Embedded** — No external dependencies
- **Portable** — Database file can be backed up or moved
- **Sufficient** — Single-writer matches single-process design
- **WAL mode** — Concurrent reads during writes

### Why track rate limits in database?

- **Survives restarts** — Won't exceed limits after crash
- **Accurate history** — Query actual request times
- **Debuggable** — Inspect rate limit state directly

---

## Performance Characteristics

### Expected Throughput

| Workload | Approximate Capacity |
|----------|---------------------|
| Dispatch rate | ~1,000-5,000 work units/minute |
| Concurrent work | Limited by services, not orchestrator |
| Queue depth | 10,000+ work units |
| SQLite queries | ~100-500/second sustainable |

### Bottlenecks

| Component | Bottleneck | Mitigation |
|-----------|------------|------------|
| Rate limit queries | `SELECT COUNT(*)` per dispatch | Index on `(service, started_at)`, prune old logs |
| Callback latency | Slow priority/dependency callbacks | 100ms timeout, cache where possible |
| SQLite writes | WAL mode helps, but still single-writer | Batch updates where possible |

For higher throughput, consider caching rate limit counts in memory with periodic SQLite sync.

---

## Platform Support

### Cross-Platform Components

| Component | Windows | macOS | Linux |
|-----------|---------|-------|-------|
| Orchestrator, queue, scheduling | ✅ | ✅ | ✅ |
| SQLite + WAL mode | ✅ | ✅ | ✅ |
| Async executor (API calls) | ✅ | ✅ | ✅ |
| Process pool executor | ✅ | ✅ | ✅ |
| Subprocess executor | ⚠️ | ✅ | ✅ |

### Process Pool on Windows

Works, but uses `spawn` instead of `fork`. Handlers must be picklable and defined at module level (good practice on all platforms).

### Subprocess Executor

Shell commands are inherently platform-specific. The orchestrator provides the mechanism (spawn, capture output, timeout); the application provides portable commands.

**For cross-platform subprocess tasks:**
- Use Python libraries instead of shell commands where possible (e.g., `asyncssh` instead of `ssh` subprocess)
- Or provide platform-specific handlers

**For SSH specifically:**
- Unix: Native `ssh` command works
- Windows: OpenSSH available since Windows 10, or use `asyncssh`/`paramiko` for guaranteed portability

---

## Limitations

This library is designed for coordinating external work. It is **not suitable** for:

- Running compute-heavy work directly in the orchestrator process
- Distributing a single queue across multiple machines
- Extremely high throughput (>10,000 dispatches/minute)
- Work that must survive total machine failure

For heavy computation, the work should run in process pools, on remote hosts, or via external APIs — and this library coordinates that work.

---

## Open Questions

These items need further discussion or depend on specific application requirements:

### Per-User Rate Limits

Should rate limits be global or per-user?
- **Global** (current design): Simpler, sufficient for single-user or internal tools
- **Per-user**: Add `user` field to `service_log`, calculate limits per user

**Decision needed**: Is per-user rate limiting a core feature or application responsibility?

### Dead Letter Queue

Should failed work (after all retries) go to a DLQ?
- **Yes**: Enables manual inspection and replay
- **No**: Application handles via `work_failed` events

**Decision needed**: Is DLQ a core feature or application pattern?

### SQLite Performance Optimization

At high throughput, rate limit queries may bottleneck:
- **Option A**: Keep current design, document limits (~500 dispatches/sec)
- **Option B**: Add in-memory token bucket cache with periodic SQLite sync
- **Option C**: Offer optional Redis backend for high-throughput users

**Decision needed**: Optimize for simplicity or throughput?

### Default Retry Values

What should the default retry policy be?
- `max_attempts`: 3?
- `base_delay`: 1 second?
- `max_delay`: 5 minutes?

**Decision needed**: Pick sensible defaults based on common use cases.

---

## Appendix: runcue-sim

A planned CLI tool for stress-testing and validating runcue without real services.

### Purpose

Validate orchestrator behavior under various conditions:
- Rate limiting accuracy under load
- Circuit breaker triggers and recovery
- Retry backoff timing
- Priority scheduling fairness
- Backpressure handling
- Recovery after simulated crashes

### Mock Services

Define simulated services with probabilistic behavior:

```yaml
services:
  mock_api:
    max_concurrent: 5
    rate_limit: 60
    rate_window: 60
    behavior:
      latency: {distribution: normal, mean: 200ms, stddev: 50ms}
      error_rate: 0.05        # 5% fail with retryable error
      timeout_rate: 0.02      # 2% hang until timeout
      permanent_error_rate: 0.01

  mock_ssh:
    max_concurrent: 1
    behavior:
      latency: {distribution: uniform, min: 500ms, max: 2000ms}
      error_rate: 0.10
      circuit_test: true      # Fails repeatedly to trigger circuit breaker
```

### Dependency Patterns

Define how mock work units relate to each other:

| Pattern | Structure | Tests |
|---------|-----------|-------|
| `none` | Independent work units | Pure throughput |
| `single` | A → B | Basic dependency resolution |
| `chain` | A → B → C → D | Three-level sequential |
| `fan_out` | A → [B, C, D] | One-to-many |
| `fan_in` | [A, B, C] → D | Many-to-one (wait for all) |
| `diamond` | A → [B, C] → D | Fan-out then fan-in |
| `complex` | Mixed patterns | Realistic workload |
| `circular` | A → B → A | Should be detected and rejected |

```yaml
workload:
  pattern: diamond
  # or
  pattern: random
  pattern_weights: {none: 0.3, single: 0.3, fan_out: 0.2, diamond: 0.2}
  
  # Pattern parameters
  fan_out_width: 5        # How many children in fan_out
  chain_depth: 4          # How deep for chain pattern
  task_types: [extract, transform, load]  # Cycle through these
```

Circular dependencies test error handling — the orchestrator should detect and fail these at submission time, not deadlock.

### Scenario Profiles

Pre-built scenarios for common test cases:

| Scenario | What it Tests |
|----------|---------------|
| `steady_load` | Sustained throughput at 80% capacity |
| `burst` | Sudden spike to 200% capacity |
| `cascade_failure` | One service fails, tests circuit breaker |
| `priority_test` | Mix of high/low priority, verify ordering |
| `starvation` | Continuous high-priority stream, verify low-priority eventually runs |
| `recovery` | Simulate crash mid-processing, verify clean restart |

### TUI Display

```text
┌─ Queue ──────────────────────────────────────────────────────────┐
│ Queued: 847    Running: 5    Completed: 1,203    Failed: 23     │
│ Backpressure: OFF           Retry pending: 12                    │
├─ Services ───────────────────────────────────────────────────────┤
│ mock_api    [████░░░░░░] 4/5 concurrent   52/60 rate   ● OK     │
│ mock_ssh    [██████████] 1/1 concurrent    -           ● OK     │
│ mock_gpu    [░░░░░░░░░░] 0/2 concurrent    -           ◐ HALF   │
├─ Recent Events ──────────────────────────────────────────────────┤
│ 12:34:56 work_completed  id=w_847  task=extract  duration=245ms │
│ 12:34:55 work_failed     id=w_832  task=analyze  error=timeout  │
│ 12:34:55 circuit_opened  service=mock_gpu  failures=5           │
│ 12:34:54 work_retrying   id=w_831  task=extract  attempt=2      │
├─ Controls ───────────────────────────────────────────────────────┤
│ [E] Error rate  [L] Latency  [R] Submit rate  [P] Pause  [Q] Quit│
└──────────────────────────────────────────────────────────────────┘
```

### Interactive Controls

Adjust parameters while running to observe system response:

| Key | Action |
|-----|--------|
| `E` | Adjust error rate (slider 0-50%) |
| `L` | Adjust mean latency |
| `R` | Adjust work submission rate |
| `C` | Trigger service failure (circuit breaker test) |
| `P` | Pause/resume submission |
| `K` | Kill a running task (simulate crash) |
| `S` | Save current metrics snapshot |

### Metrics Tracked

- Throughput (work units/minute)
- Latency percentiles (p50, p95, p99)
- Retry rate and success rate after retry
- Time spent rate-limited per service
- Circuit breaker state transitions
- Priority inversion incidents (low-priority finishing before older high-priority)
- Queue depth over time

### Usage

```bash
# Run with built-in scenario
runcue-sim --scenario burst --duration 5m

# Run with custom config
runcue-sim --config my_test.yaml --tui

# Headless mode for CI
runcue-sim --scenario steady_load --duration 10m --report results.json
```

This tool validates the design without requiring real API keys or external services.

---

## Summary

| Component | Responsibility |
|-----------|----------------|
| **Application** | Manages artifacts, provides priority/dependency callbacks, handles UI |
| **runcue (Cue)** | Schedules work, tracks services, reports progress |
| **Resource Broker** | Enforces rate limits, manages circuit breakers, handles leasing |
| **External Systems** | AI APIs, SSH hosts, process pools — where work actually runs |
| **SQLite** | Persists queue, rate limit history, and lease state |

```text
Application                    runcue                      External
    │                            │                            │
    │  cue.submit(task, target)  │                            │
    │ ─────────────────────────► │                            │
    │                            │  (queue, check deps,       │
    │                            │   check rate limits)       │
    │                            │                            │
    │                            │  handler(work)             │
    │                            │ ──────────────────────────►│
    │                            │                            │
    │                            │           result           │
    │                            │ ◄──────────────────────────│
    │   event: completed         │                            │
    │ ◄───────────────────────── │                            │
```

The application submits work. runcue decides when to dispatch based on dependencies, rate limits, and priorities. Events flow back as work completes. The actual computation runs elsewhere.
