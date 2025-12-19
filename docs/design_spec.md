# runcue Design Specification

**Status**: Design Specification (Dec 2025)

> **runcue** is a control tower for coordinating work across rate-limited services. It decides **when** work can run. You decide **what** it does and **where** outputs go.

A Python library for scheduling tasks that call APIs, run commands, or process data. runcue handles rate limiting, concurrency, and dependency checking—but never stores anything. Your artifacts are the source of truth.

---

## The Core Idea

runcue schedules work based on **artifact validity**, not task completion:

```text
Traditional task queues:     "Task B depends on Task A completing"
runcue:                      "Task B is ready when its inputs are valid"
```

This means:

- **Re-run one step**: Just submit it—if inputs exist, it runs immediately
- **Inputs go stale**: `is_ready` blocks the consumer, `is_stale` triggers re-run of producer
- **Crash recovery**: Resubmit everything, `is_stale` skips what's done
- **Side effects**: Proof artifacts prevent duplicates

---

## Architecture

```text
┌─────────────────────────────────────────────────────────────────┐
│                      Your Application                           │
│                                                                 │
│   is_ready(work)  ──→ "Are inputs valid? Can this run?"        │
│   is_stale(work)  ──→ "Is output missing/outdated? Re-run?"    │
│   task handlers   ──→ "Here's how to do the work"              │
│   callbacks       ──→ "Log, track, alert (your storage)"       │
│                                                                 │
└────────────────────────────▲────────────────────────────────────┘
                             │
                             │  "Ready?" → Yes/No
                             │  "Stale?" → Yes/No
                             │  "Go!"    → handler called
                             │
┌────────────────────────────┴────────────────────────────────────┐
│                          runcue                                 │
│                                                                 │
│   Work Queue (in-memory)                                        │
│   Rate Limit Tracking (in-memory)                               │
│   Scheduling Loop                                               │
│   Event Emission                                                │
│                                                                 │
│   NO PERSISTENCE - artifacts are the source of truth            │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

**Design principle**: runcue is stateless. It doesn't store work history, results, or artifact information. All state is derived from your artifacts via the callbacks you provide. When you restart, resubmit your work—`is_stale` will skip anything that's already complete.

---

## Core Concepts

### Work Units

A work unit is a request to perform work:

| Field | Description |
|-------|-------------|
| `id` | Unique identifier (auto-generated) |
| `task` | Task type name (references a registered handler) |
| `params` | Dict of parameters for the handler |
| `attempt` | Current attempt number (for retries) |

When a handler runs, it receives a `work` object with these fields.

### Services

Services are **rate-limit buckets**—they don't execute anything, just track usage in memory:

```python
cue.service("openai", rate="60/min", concurrent=5)   # 60 req/min, 5 at once
cue.service("local", concurrent=4)                    # No rate limit, 4 parallel
cue.service("ssh_prod", concurrent=1)                 # Serial access only
```

When work uses a service, runcue ensures limits aren't exceeded before calling the handler.

Rate formats:
- `"60/min"` → 60 per minute
- `"1000/hour"` → 1000 per hour
- `"10/sec"` → 10 per second

### Tasks

Tasks define handlers for categories of work:

```python
@cue.task("extract_text", uses="openai", retry=3)
def extract_text(work):
    text = call_api(work.params["image_path"])
    save_text(work.params["output_path"], text)
    return {"success": True, "length": len(text)}
```

The `uses` parameter ties the task to a service for rate limiting. The handler owns all the logic—API calls, file operations, error handling.

### Artifacts and Dependencies

**runcue never touches artifacts.** It doesn't know if they're files, database rows, S3 objects, or Redis keys. Instead, you provide two callbacks:

#### is_ready: "Can this work run?"

Called before scheduling. Returns `True` if all inputs are valid:

```python
@cue.is_ready
def is_ready(work) -> bool:
    if work.task == "extract_text":
        return Path(work.params["image_path"]).exists()
    if work.task == "summarize":
        return all(Path(p).exists() for p in work.params["text_paths"])
    return True
```

If `is_ready` returns `False`, the work stays pending and is rechecked periodically.

#### is_stale: "Should this run?"

Called for pending work. Returns `True` if outputs are missing or outdated:

```python
@cue.is_stale
def is_stale(work) -> bool:
    if work.task == "extract_text":
        output = work.params["image_path"].replace(".png", ".txt")
        if not Path(output).exists():
            return True  # Missing
        # Check freshness
        age = time.time() - Path(output).stat().st_mtime
        return age > 86400  # Stale if older than 24h
    return True  # Default: run if ready
```

If `is_stale` returns `False`, the work is skipped (output already valid).

### The Validation Pattern

Define validators once, use in both checks:

```python
MAX_AGE = 86400  # 24 hours

def text_is_valid(text_path: str) -> bool:
    """Is this text artifact valid?"""
    path = Path(text_path)
    if not path.exists():
        return False
    age = time.time() - path.stat().st_mtime
    return age < MAX_AGE

@cue.is_ready
def is_ready(work) -> bool:
    if work.task == "summarize":
        # Consumer: check inputs are valid
        return all(text_is_valid(p) for p in work.params["text_paths"])
    return True

@cue.is_stale
def is_stale(work) -> bool:
    if work.task == "extract_text":
        # Producer: check output is valid
        output = work.params["image_path"].replace(".png", ".txt")
        return not text_is_valid(output)
    return True
```

This creates automatic refresh: if a text file becomes stale, downstream tasks block (`is_ready` fails) and the producer re-runs (`is_stale` is True).

---

## No Persistence

runcue stores nothing. This is intentional:

| What | Where It Lives |
|------|----------------|
| Work queue | In-memory (lost on restart) |
| Rate limit state | In-memory (reset on restart) |
| Work history | Your callbacks (your storage) |
| "Is work done?" | Your `is_stale` callback (checks artifacts) |

### On Restart

Just resubmit all work:

```python
cue.start()
for doc in documents:
    await cue.submit("process", params={"path": doc})
```

`is_stale` will return `False` for work that's already done, skipping it. `is_ready` will block work whose inputs don't exist yet. The system self-heals.

### Rate Limits on Restart

Rate limit tracking resets on restart. If you were 45/60 calls into a minute-window, you might briefly exceed the limit after restart. For most APIs, this is fine—they have their own enforcement. If you need persistent rate tracking, implement it in your callbacks.

---

## Callbacks for History and Metrics

runcue emits events. You decide what to do with them:

```python
@cue.on_complete
def on_complete(work, result, duration):
    """Called after successful completion."""
    logging.info(f"{work.task} completed in {duration:.2f}s")
    my_db.insert("history", work.id, result, duration)
    metrics.histogram("duration", duration, task=work.task)

@cue.on_failure
def on_failure(work, error, will_retry):
    """Called after failure."""
    logging.error(f"{work.task} failed: {error}")
    if not will_retry:
        alerting.send(f"Permanent failure: {work.task}")

@cue.on_skip
def on_skip(work):
    """Called when is_stale returned False (work skipped)."""
    logging.debug(f"Skipped {work.task} - already done")

@cue.on_start
def on_start(work):
    """Called when work begins executing."""
    metrics.increment("started", task=work.task)
```

This lets you:
- Log to files, databases, or cloud services
- Track costs and durations
- Send alerts on failures
- Build dashboards
- Cache artifact status for faster `is_ready`/`is_stale` checks

The community can build complementary packages for common patterns.

---

## Scheduling

The scheduling loop:

```text
1. Get pending work from in-memory queue
2. For each work unit:
   a. Call is_ready(work) → False? Stay pending
   b. Call is_stale(work) → False? Skip (mark done)
   c. Check service limits → Exceeded? Wait
   d. All pass? Call handler
3. On completion: emit on_complete
4. On failure: emit on_failure, maybe retry
5. Repeat
```

### Priority

Optional callback for scheduling order:

```python
@cue.priority
def prioritize(ctx) -> float:
    if ctx.work.params.get("urgent"):
        return 1.0
    # Older work gets higher priority (starvation prevention)
    return min(0.3 + ctx.wait_time / 3600, 0.9)
```

Returns 0.0 (lowest) to 1.0 (highest). Default is FIFO with starvation prevention.

### Rate Limiting

In-memory tracking per service:

- Count active requests (for concurrency limit)
- Count recent requests (for rate limit)
- Block new work until limits allow

When work completes, release the service slot.

---

## Artifacts as Proof

For side effects (email, payments, deployments), **create proof that the action happened**:

```python
@cue.task("send_notification", uses="email")
def send_notification(work):
    order_id = work.params["order_id"]
    proof_path = f"proof/{order_id}/notification.json"
    
    # Check proof first (idempotent)
    if Path(proof_path).exists():
        return json.loads(Path(proof_path).read_text())
    
    # Send the email
    result = send_email(
        to=work.params["email"],
        subject=f"Order {order_id} shipped"
    )
    
    # Create proof artifact
    Path(proof_path).parent.mkdir(parents=True, exist_ok=True)
    Path(proof_path).write_text(json.dumps({
        "sent_at": time.time(),
        "message_id": result.message_id
    }))
    
    return {"message_id": result.message_id}

@cue.is_stale
def is_stale(work) -> bool:
    if work.task == "send_notification":
        proof = f"proof/{work.params['order_id']}/notification.json"
        return not Path(proof).exists()
    return True
```

Now the notification is idempotent—resubmitting won't send duplicates.

---

## API Reference

### Cue

```python
cue = runcue.Cue()                    # Create coordinator

cue.service(name, rate=, concurrent=) # Register service
cue.start()                           # Start scheduling loop
await cue.stop()                      # Graceful shutdown

# Decorators
@cue.task(name, uses=service, retry=n)    # Register task handler
@cue.is_ready                              # Readiness callback
@cue.is_stale                              # Staleness callback
@cue.priority                              # Priority callback
@cue.on_complete                           # Completion callback
@cue.on_failure                            # Failure callback
@cue.on_skip                               # Skip callback
@cue.on_start                              # Start callback

# Submission
work_id = await cue.submit(task, params={})
```

### Work Object

Available in handlers and callbacks:

```python
work.id          # Unique ID
work.task        # Task type name  
work.params      # Dict of parameters
work.attempt     # Current attempt (1, 2, 3...)
```

### Service Configuration

```python
cue.service("name",
    rate="60/min",       # Rate limit (requests/period)
    concurrent=5,        # Max parallel requests
)
```

Rate formats: `"60/min"`, `"1000/hour"`, `"10/sec"`

---

## Retry and Failure Handling

```python
@cue.task("flaky_api", uses="external", retry=3)
def call_flaky_api(work):
    response = requests.post(url, json=work.params)
    response.raise_for_status()
    return response.json()
```

Retry policy:
- `retry=N`: Max N attempts
- Exponential backoff between attempts
- Transient errors (timeout, 5xx) trigger retry
- Permanent errors (4xx, validation) fail immediately

After exhausting retries, `on_failure` is called with `will_retry=False`.

---

## Design Decisions

### Why no persistence?

Artifacts are the source of truth. If you track completion in a database AND in artifacts, they can get out of sync. By making artifacts the only source, there's one truth.

If you need persistent queues (work survives restart without resubmission), add your own via callbacks—but consider whether you really need it. Most batch jobs can just resubmit.

### Why artifact-based dependencies?

Traditional queues: "B depends on A completing"
runcue: "B is ready when its inputs are valid"

Benefits:
- **Partial re-runs**: Submit any step—if inputs exist, it runs
- **Crash recovery**: Resubmit everything, `is_stale` skips completed
- **Flexible storage**: Files, databases, S3—runcue doesn't care
- **Staleness handling**: Automatic refresh when inputs expire

### Why callbacks for everything?

You know your infrastructure. Maybe you log to:
- Files
- PostgreSQL
- Datadog
- CloudWatch
- Elasticsearch

runcue doesn't choose for you. Callbacks let you integrate with whatever you use.

### Why embedded (not a service)?

runcue runs in your process:
- No Redis/RabbitMQ/external dependencies
- No deployment complexity
- No network latency for scheduling decisions
- Easy testing and debugging

Trade-off: Not distributed. For horizontal scaling, run multiple instances with different workloads.

---

## Comparison to Other Tools

| Tool | Dependency Model | runcue Difference |
|------|------------------|-------------------|
| Make | File timestamps | runcue adds rate limiting, dynamic at runtime |
| Luigi | Target.exists() | runcue is simpler (callbacks), no server, no persistence |
| Celery | Task chains via broker | runcue is embedded, stateless, artifact-based |
| Airflow | DAG with scheduler | runcue is a library, not a platform |

runcue is closest to **Make** (artifact-based) and **Luigi** (target existence), but simpler and fully embedded.

---

## Performance Characteristics

| Metric | Expectation |
|--------|-------------|
| Dispatch rate | 1,000+ work/minute |
| Concurrent work | Limited by services |
| Queue depth | 10,000+ work units |
| Callback latency | Should be <100ms |

Bottlenecks:
- Slow `is_ready`/`is_stale` callbacks (keep them fast)
- Slow handlers (that's your code)
- Memory for large queues

---

## Limitations

runcue is designed for coordinating work in a single process. Not suitable for:

- **Distributed queues**: Work shared across machines
- **Durable queues**: Work must survive restart (use callbacks if needed)
- **Very high throughput**: >10k tasks/minute needs optimization
- **Request-based web apps**: Each HTTP request is independent

For these, consider Celery, Temporal, or cloud workflow services.

---

## Summary

| Concept | What It Means |
|---------|---------------|
| **Work unit** | A request to do something |
| **Task** | Handler function for a type of work |
| **Service** | Rate-limit bucket (in-memory) |
| **Artifact** | Output of work (managed by you) |
| **is_ready** | "Can this work run?" (inputs valid) |
| **is_stale** | "Should this run?" (outputs invalid) |
| **Callbacks** | History, metrics, alerts (your storage) |

```text
Your App                       runcue                    Your Handlers
    │                            │                            │
    │  submit(task, params)      │                            │
    │ ─────────────────────────► │                            │
    │                            │  is_ready? is_stale?       │
    │  ◄─────────────────────────│  (your callbacks)          │
    │                            │                            │
    │                            │  handler(work)             │
    │                            │ ──────────────────────────►│
    │                            │                            │
    │                            │        result              │
    │                            │ ◄──────────────────────────│
    │  on_complete callback      │                            │
    │ ◄───────────────────────── │                            │
```

runcue handles the **when**. You handle the **what** and **where**.
