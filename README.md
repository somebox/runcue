# runcue

[![Tests](https://github.com/somebox/runcue/actions/workflows/test.yml/badge.svg?branch=main)](https://github.com/somebox/runcue/actions/workflows/test.yml)

![runcue logo](./docs/images/runcue-logo-sm.png)

A Python library that makes it easy to run background requests with dependencies across rate-limited services, without any extra infrastructure or separate job queues. Define your tasks, tell runcue when they're ready to run, and let it handle the scheduling and throttling. 

## The Idea

When you're building applications that call external APIs, run shell commands, or process data through multiple steps, you face common challenges:

- **Rate limits**: APIs only allow N requests per minute
- **Concurrency**: You can only run M things at once
- **Dependencies**: Step B needs the output from Step A

runcue handles the **when**. You handle the **what**.

```text
┌─────────────────────────────────────────────────────────────┐
│                      Your Application                       │
│                                                             │
│   is_ready(work)  ──→ "Can this run? Are inputs valid?"    │
│   is_stale(work)  ──→ "Should this re-run? Output valid?"  │
│   task handlers   ──→ "Here's how to do the actual work"   │
│                                                             │
└────────────────────────▲────────────────────────────────────┘
                         │
                         │  "Ready?"  "Stale?"  "Go!"
                         │
┌────────────────────────┴────────────────────────────────────┐
│                         runcue                              │
│                                                             │
│   Queue work  →  Check readiness  →  Respect limits  →     │
│   Call handlers  →  Emit events  →  Report results         │
│                                                             │
└─────────────────────────────────────────────────────────────┘
```

## How It Runs

runcue is **embedded in your application** and **fully in-memory**. No external services, no databases, no cleanup.

- **No external dependencies**: No Redis, no RabbitMQ, no SQLite
- **Stateless**: Artifacts are the source of truth, not runcue
- **Single process**: Runs in your app's asyncio event loop
- **Lightweight**: Just Python, nothing else

**What about persistence?**

runcue doesn't store anything. Your **artifacts** are the source of truth:

- "Did this run?" → Check if the output artifact exists
- "Should this re-run?" → Check if the output is stale
- "Can this start?" → Check if the inputs exist

On restart, just resubmit your work. `is_stale` will skip anything that's already done.

**What about history and metrics?**

Use callbacks. You decide where data goes:

```python
@cue.on_complete
def track(work, result, duration):
    my_metrics.record(work.task, duration)
    my_database.log(work.id, result)

@cue.on_failure
def alert(work, error):
    slack.post(f"Failed: {work.task} - {error}")
```

This keeps runcue simple. The community can build complementary packages for logging, caching artifact status, metrics dashboards, etc.

**Ideal for:**

- CLI tools and batch processors
- Scripts that coordinate calls to multiple APIs
- Local applications that push work to subprocesses
- Background workers that run continuously

**Not designed for:**

- Distributed systems that need work shared across machines
- Scenarios where queued work must survive process restart
- Request-based web applications

If you need distributed task queues, look at Celery. If you need durable queues, add your own persistence via callbacks.

## What is "Work"?

A **work unit** is a single thing to be done. When you call `cue.submit()`, you create a work unit. When runcue calls your handler, it passes that work unit.

```python
# Submit work
work_id = await cue.submit("extract_text", params={
    "page_path": "/data/page-001.png",
    "settings": {"language": "en"}
})

# Your handler receives it
@cue.task("extract_text", uses="openai")
def extract_text(work):
    # work.id        - unique identifier
    # work.task      - "extract_text"
    # work.params    - {"page_path": "...", "settings": {...}}
    # work.attempt   - which attempt this is (1, 2, 3...)
    
    page_path = work.params["page_path"]
    # ... do the work ...
    return {"text": extracted_text}
```

**What's in a work unit:**

| Field | Description |
|-------|-------------|
| `work.id` | Unique identifier |
| `work.task` | Task type name |
| `work.params` | Parameters you passed to `submit()` |
| `work.attempt` | Current attempt number (for retries) |

## Quick Example

```python
import runcue

cue = runcue.Cue()

# Define rate-limited services
cue.service("openai", rate="60/min", concurrent=5)

# Define a task
@cue.task("extract_text", uses="openai")
def extract_text(work):
    text = call_openai_vision(work.params["image_path"])
    output_path = work.params["image_path"].replace(".png", ".txt")
    Path(output_path).write_text(text)
    return {"output": output_path}

# Tell runcue when work can start
@cue.is_ready
def is_ready(work):
    if work.task == "extract_text":
        return Path(work.params["image_path"]).exists()
    return True

# Tell runcue when to skip (output already valid)
@cue.is_stale
def is_stale(work):
    if work.task == "extract_text":
        output = work.params["image_path"].replace(".png", ".txt")
        return not Path(output).exists()
    return True  # Default: run if ready

# Run
async def main():
    cue.start()
    await cue.submit("extract_text", params={"image_path": "page.png"})
    # ... submit more work ...
    await cue.stop()
```

## Core Concepts

### Producers, Consumers, and Artifacts

Every task is a **producer** (creates output) and often a **consumer** (needs input from other tasks).

```text
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  split_pdf   │────────▶│ extract_text │────────▶│  summarize   │
│   (producer) │ pages   │  (both)      │ text    │  (consumer)  │
└──────────────┘         └──────────────┘         └──────────────┘
```

**An artifact is proof that work completed correctly.** It's not just an output file—it's evidence. The artifact might be:
- A file on disk
- A database record  
- A remote API response
- A flag that says "this happened"

For side-effect tasks (sending email, charging a card), **you must create proof**:

```python
@cue.task("send_email")
def send_email(work):
    email_service.send(to=work.params["to"], body=work.params["body"])
    
    # Create proof artifact
    proof = f"sent/{work.params['order_id']}.json"
    Path(proof).write_text(json.dumps({"sent_at": time.time()}))
    return {"proof": proof}
```

### Services: Rate Limit Buckets

Services define constraints. They don't execute anything—they're buckets that track usage:

```python
cue.service("openai", rate="60/min", concurrent=5)   # 60 req/min, 5 parallel
cue.service("knife", concurrent=1)                    # Serial access only
cue.service("email", rate="100/hour")                 # Rate limited
```

### The Two Checks

runcue asks your code two questions:

| Check | Question | When Asked |
|-------|----------|------------|
| `is_ready` | "Can this start? Are inputs valid?" | Before dispatching |
| `is_stale` | "Is the output missing/invalid?" | To decide if work should run |

Both checks use artifact validation. Define validation once, use it in both:

```python
# Define artifact validators once
def text_valid(path):
    """Is this text file valid and fresh?"""
    p = Path(path)
    if not p.exists():
        return False
    # Check freshness: not older than 24 hours
    return (time.time() - p.stat().st_mtime) < 86400

# Use in both checks
@cue.is_ready
def is_ready(work) -> bool:
    """Can this work start? Are inputs valid?"""
    if work.task == "summarize":
        return all(text_valid(p) for p in work.params["text_paths"])
    return True

@cue.is_stale
def is_stale(work) -> bool:
    """Is output invalid? Should this run?"""
    if work.task == "extract_text":
        output = work.params["image_path"].replace(".png", ".txt")
        return not text_valid(output)
    return True  # Default: run if ready
```

### Callbacks for History and Metrics

runcue doesn't store history—you do, via callbacks:

```python
@cue.on_complete
def on_complete(work, result, duration):
    """Called after each successful completion."""
    logging.info(f"{work.task} completed in {duration:.2f}s")
    my_metrics.histogram("task_duration", duration, tags={"task": work.task})

@cue.on_failure
def on_failure(work, error):
    """Called after each failure."""
    logging.error(f"{work.task} failed: {error}")
    alerting.send(f"Failed: {work.task}")

@cue.on_skip
def on_skip(work):
    """Called when work is skipped (is_stale returned False)."""
    logging.debug(f"{work.task} skipped - output already valid")
```

This lets you:
- Log to files, databases, or cloud services
- Track costs and durations
- Send alerts on failures
- Build dashboards
- Cache artifact status for faster `is_ready` checks

## Examples

### Example 1: Apple Pie Factory

A bakery system: slice apples → make pie. Only one knife (serial slicing), slices go stale after 24 hours.

```python
cue = runcue.Cue()
cue.service("knife", concurrent=1)    # One knife, serial
cue.service("oven", concurrent=2)     # Two ovens

MAX_SLICE_AGE = 86400  # 24 hours

def slices_valid(job_id):
    path = Path(f"prep/{job_id}/slices.json")
    if not path.exists():
        return False
    data = json.loads(path.read_text())
    return (time.time() - data["sliced_at"]) < MAX_SLICE_AGE

@cue.task("slice_apples", uses="knife")
def slice_apples(work):
    slices = cut_apples(work.params["apple_ids"])
    Path(f"prep/{work.params['job_id']}/slices.json").write_text(
        json.dumps({"slices": slices, "sliced_at": time.time()})
    )
    return {"count": len(slices)}

@cue.task("make_pie", uses="oven")
def make_pie(work):
    slices = load_slices(work.params["job_id"])
    bake_pie(slices)
    return {"pie": f"output/{work.params['job_id']}/pie.json"}

@cue.is_ready
def is_ready(work):
    if work.task == "make_pie":
        return slices_valid(work.params["job_id"])
    return True

@cue.is_stale
def is_stale(work):
    if work.task == "slice_apples":
        return not slices_valid(work.params["job_id"])
    return True
```

If slices are stale, `make_pie` waits (`is_ready` fails) while `slice_apples` re-runs (`is_stale` is True).

### Example 2: PDF Processing Pipeline

Split PDF → extract text per page → summarize.

```python
cue = runcue.Cue()
cue.service("local", concurrent=4)
cue.service("openai", rate="60/min", concurrent=5)

@cue.task("split_pdf", uses="local")
def split_pdf(work):
    pages = pdf_to_images(work.params["pdf_path"])
    for i, page in enumerate(pages):
        page.save(f"work/{work.params['job_id']}/pages/{i:03d}.png")
    return {"page_count": len(pages)}

@cue.task("extract_text", uses="openai")
def extract_text(work):
    text = call_openai_vision(work.params["page_path"])
    text_path = work.params["page_path"].replace(".png", ".txt")
    Path(text_path).write_text(text)
    return {"text_path": text_path}

@cue.is_ready
def is_ready(work):
    if work.task == "extract_text":
        return Path(work.params["page_path"]).exists()
    return True

@cue.is_stale
def is_stale(work):
    if work.task == "extract_text":
        output = work.params["page_path"].replace(".png", ".txt")
        return not Path(output).exists()
    return True
```

**Reprocess pages 5-10:**

```python
for i in range(5, 11):
    await cue.submit("extract_text", params={
        "page_path": f"work/{job_id}/pages/{i:03d}.png"
    })
# Pages exist—runs immediately. No need to re-split.
```

### Example 3: Order Fulfillment with Proof Artifacts

Process order → charge payment → ship → send email. Side effects need proof.

```python
cue = runcue.Cue()
cue.service("payment", rate="100/min")
cue.service("email", rate="50/min")

@cue.task("charge_payment", uses="payment")
def charge_payment(work):
    order_id = work.params["order_id"]
    proof_path = Path(f"orders/{order_id}/payment.json")
    
    # Idempotent: check proof first
    if proof_path.exists():
        return json.loads(proof_path.read_text())
    
    result = payment_api.charge(
        amount=work.params["amount"],
        idempotency_key=order_id
    )
    
    proof_path.write_text(json.dumps({
        "charged_at": time.time(),
        "transaction_id": result.id
    }))
    return {"transaction_id": result.id}

@cue.task("send_confirmation", uses="email")
def send_confirmation(work):
    order_id = work.params["order_id"]
    proof_path = Path(f"orders/{order_id}/email_sent.json")
    
    if proof_path.exists():
        return json.loads(proof_path.read_text())
    
    email_api.send(to=work.params["email"], template="shipped")
    proof_path.write_text(json.dumps({"sent_at": time.time()}))
    return {"sent": True}

@cue.is_ready
def is_ready(work):
    order_id = work.params["order_id"]
    if work.task == "send_confirmation":
        return Path(f"orders/{order_id}/payment.json").exists()
    return True

@cue.is_stale
def is_stale(work):
    order_id = work.params["order_id"]
    if work.task == "charge_payment":
        return not Path(f"orders/{order_id}/payment.json").exists()
    if work.task == "send_confirmation":
        return not Path(f"orders/{order_id}/email_sent.json").exists()
    return True
```

## API Overview

```python
cue = runcue.Cue(
    # For simple pipelines - timeout individual work items:
    pending_warn_after=30,  # Optional: warn about work pending > 30s
    pending_timeout=300,    # Optional: auto-fail work pending > 5 min
    
    # For batch jobs - timeout on system stall:
    stall_warn_after=30,    # Optional: warn if no progress for 30s
    stall_timeout=60,       # Optional: fail all if stalled > 60s
)

# Services
cue.service("name", rate="N/min", concurrent=M)

# Tasks
@cue.task("name", uses="service")
def handler(work):
    return {"result": ...}

# Artifact checks
@cue.is_ready
def is_ready(work) -> bool: ...

@cue.is_stale
def is_stale(work) -> bool: ...

# Event callbacks (optional)
@cue.on_complete
def on_complete(work, result, duration): ...

@cue.on_failure
def on_failure(work, error): ...

@cue.on_skip
def on_skip(work): ...

@cue.on_start
def on_start(work): ...

# Submit work
work_id = await cue.submit("task", params={...})

# Lifecycle
cue.start()        # Start background scheduling
await cue.stop()   # Graceful shutdown
```

## Debugging

### Common Pitfalls

**1. Waiting for artifacts that will never exist**

If `is_ready` waits for an artifact that no task produces, work blocks forever:

```python
# BAD: summarize waits for "analysis.json" but nothing creates it
@cue.is_ready
def is_ready(work):
    if work.task == "summarize":
        return Path("analysis.json").exists()  # Who creates this?
    return True

# GOOD: Make sure some task actually produces the artifact
@cue.task("analyze")
def analyze(work):
    result = do_analysis(work.params["input"])
    Path("analysis.json").write_text(json.dumps(result))  # Creates the artifact
    return result

@cue.is_ready
def is_ready(work):
    if work.task == "summarize":
        return Path("analysis.json").exists()  # Now "analyze" creates this
    return True
```

**2. Circular dependencies**

```python
# BAD: A waits for B, B waits for A → deadlock
@cue.is_ready
def is_ready(work):
    if work.task == "task_a":
        return Path("b_output.txt").exists()  # A needs B's output
    if work.task == "task_b":
        return Path("a_output.txt").exists()  # B needs A's output
    return True
```

**3. Stale downstream artifacts**

When you re-run an upstream task, downstream artifacts may contain outdated data:

```python
# Scenario: extract → transform → load
# If you re-run "extract", the "transform" output is now stale

# Solution: Check input freshness in is_stale
@cue.is_stale
def is_stale(work):
    if work.task == "transform":
        input_path = work.params["input"]
        output_path = work.params["output"]
        
        if not Path(output_path).exists():
            return True
        
        # Re-run if input is newer than output
        input_mtime = Path(input_path).stat().st_mtime
        output_mtime = Path(output_path).stat().st_mtime
        return input_mtime > output_mtime
    
    return True
```

**4. Side effects without proof**

Tasks that send emails, charge cards, or call external APIs need proof artifacts:

```python
# BAD: No proof that email was sent - might send twice on restart
@cue.task("send_email")
def send_email(work):
    email_api.send(to=work.params["to"], body=work.params["body"])
    return {"sent": True}

# GOOD: Record the send, check before sending
@cue.task("send_email")
def send_email(work):
    order_id = work.params["order_id"]
    
    # Check if already sent
    existing = db.get_email_record(order_id)
    if existing:
        return existing
    
    # Send and record
    email_api.send(to=work.params["to"], body=work.params["body"])
    record = {"order_id": order_id, "sent_at": time.time()}
    db.save_email_record(record)
    return record
```

### Diagnosing Stalled Work

When work appears stuck, use `debug_blocked()` to understand why:

```python
# Check what's blocking
for item in cue.debug_blocked():
    work = item["work"]
    reason = item["reason"]    # 'not_ready', 'service_full', 'unknown_task'
    details = item["details"]
    print(f"{work.task}: {reason} - {details}")
```

Reasons work might be blocked:
- **not_ready**: `is_ready` returned `False` - check if input artifacts exist
- **service_full**: Service at capacity - wait or increase limits
- **unknown_task**: Task handler not registered

### Preventing Infinite Hangs

There are two timeout strategies depending on your use case:

**1. Pending Timeout** - for simple pipelines where individual work shouldn't wait long:

```python
# Fail any work pending > 5 minutes
cue = runcue.Cue(pending_warn_after=30, pending_timeout=300)
```

**2. Stall Timeout** - for batch jobs where work legitimately queues while earlier work runs:

```python
# Only fail if the SYSTEM stops making progress for 60s
cue = runcue.Cue(stall_warn_after=30, stall_timeout=60)
```

The key difference:
- `pending_timeout` fails work based on how long it's been **queued**
- `stall_timeout` fails work based on how long since **any work completed**

For batch processing (like the mandelbrot example), use `stall_timeout` - work legitimately waits in queue while earlier tiles compute.

```python
@cue.on_failure
def on_failure(work, error):
    if isinstance(error, TimeoutError):
        print(f"Work {work.task} timed out")

# Optional: custom stall warning (default prints to stderr)
@cue.on_stall_warning
def on_stall_warning(seconds_since_progress, pending_count):
    print(f"⚠ No progress for {seconds_since_progress:.0f}s, {pending_count} pending")
```

Both strategies catch:
- Upstream task failures leaving downstream tasks blocked
- `is_ready` that will never return `True`
- External changes invalidating queue state
- Bugs in dependency logic

### Testing with runcue-sim

![Example output](./docs/images/simulator.png)

The simulator (`runcue-sim`) helps test your dependency patterns without real services:

```bash
# Basic simulation
runcue-sim --scenario single_queue --count 100

# Test with errors and latency
runcue-sim --scenario pipeline --count 50 --latency 200 --error-rate 0.1

# Verbose event log for debugging
runcue-sim --scenario dynamic --count 10 -v

# List available scenarios
runcue-sim --list-scenarios
```

**Built-in scenarios:**

| Scenario | Description |
|----------|-------------|
| `single_queue` | Independent tasks, one service |
| `pipeline` | Extract → Transform → Load chain |
| `fanout` | Split → parallel process → aggregate |
| `dynamic` | Complex dependencies with rebuild cycles |

**Verbose mode** shows every event:

```
TIME           EVENT        TASK             WORK_ID              DETAILS
--------------------------------------------------------------------------------
20:55:11.216 + queued       [api_fetch]      api_api_006          api_006
20:55:11.241 ✓ completed    [api_fetch]      24adb191b9b2         24ms
20:55:11.308 ⟳ invalidated  [api_fetch]      api_006              checker
```

When the simulator detects a stall (nothing completing, work still queued), it automatically shows what's blocked:

```
⚠️  Stall detected! 15 work items blocked:
    local_process: not_ready - is_ready returned False for...
```

## Why This Design?

**Traditional task queues:** "Task B depends on Task A completing."

**runcue:** "Task B is ready when its inputs are valid."

| Scenario | Traditional Queue | runcue |
|----------|-------------------|--------|
| Re-run one step | Re-run entire chain | Just that step (inputs exist) |
| Input goes stale | Manual intervention | `is_ready` blocks, `is_stale` triggers refresh |
| Crash mid-job | Replay from checkpoint | Resubmit all, `is_stale` skips completed |
| Side effects | Hope for idempotency | Proof artifacts prevent duplicates |

**Core principles:**

1. **Artifacts are proof.** Every task produces verifiable evidence of completion.
2. **Validation is centralized.** Define validators once, use in both `is_ready` and `is_stale`.
3. **runcue is stateless.** It asks "ready?" and "stale?"—you answer based on your artifacts.
4. **You own your data.** History, metrics, and caching via callbacks.

## Compared to Other Tools

| Tool | Model | runcue Difference |
|------|-------|-------------------|
| **Make** | File timestamps | runcue adds rate limiting, dynamic at runtime |
| **Luigi** | Target.exists() | runcue is simpler (callbacks, not classes), no server |
| **Celery** | Broker + task chains | runcue is embedded, stateless, artifact-based |
| **Airflow** | DAG scheduler | runcue is a library, not a platform |

runcue is closest to **Make** (artifact-based dependencies) but embedded in your app with first-class rate limiting.

## Installation

```bash
pip install runcue
```

## Contributing

Contributions welcome! Areas of interest:
- Complementary packages for logging, metrics, caching
- Example applications and patterns
- Performance optimizations

## License

MIT
