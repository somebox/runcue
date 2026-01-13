# runcue

## A Different Way to Think About Background Jobs

---

class: center, middle

# The Problem

---

## What We're Solving

When building applications that:

- **Call external APIs** with rate limits
- **Process data** through multiple steps  
- **Run commands** that depend on each other

We face common challenges:

```
😤 Rate limits     → "429 Too Many Requests"
😤 Concurrency     → Only M things can run at once
😤 Dependencies    → Step B needs output from Step A
😤 Crash recovery  → Where were we?
😤 Partial re-runs → "Just redo pages 5-10"
```

---

## The Traditional Approach

Set up infrastructure:

```
┌─────────────┐     ┌─────────────┐     ┌─────────────┐
│   Redis /   │────▶│   Celery /  │────▶│  Your App   │
│  RabbitMQ   │     │  Sidekiq    │     │  Workers    │
└─────────────┘     └─────────────┘     └─────────────┘
        │                   │
        ▼                   ▼
   Message Broker      Task Queues
   Persistence         Retry Logic
   Monitoring          Result Backend
```

**You need:**
- Message broker (Redis, RabbitMQ)
- Task queue framework (Celery, Sidekiq, Bull)
- Result backend
- Monitoring dashboard
- Configuration for all of the above

---

## Traditional: Task Dependencies

```python
# Celery chain - task B depends on task A "completing"
from celery import chain

result = chain(
    extract_text.s(page_id),
    transform_data.s(),
    summarize.s()
)()
```

**The mental model:**

> "Task B depends on Task A completing"

What if:
- Task A completed but produced bad output?
- You need to re-run just Task B?
- Task A's output became stale?

---

## Traditional: The Complexity

```python
# Celery setup - celery.py
app = Celery('myapp')
app.config_from_object('django.conf:settings', namespace='CELERY')

# settings.py
CELERY_BROKER_URL = 'redis://localhost:6379/0'
CELERY_RESULT_BACKEND = 'redis://localhost:6379/0'
CELERY_TASK_SERIALIZER = 'json'
CELERY_RESULT_SERIALIZER = 'json'
CELERY_ACCEPT_CONTENT = ['json']
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_WORKER_PREFETCH_MULTIPLIER = 1
CELERY_BROKER_CONNECTION_RETRY_ON_STARTUP = True

# Rate limiting? Manual implementation
@app.task(rate_limit='10/m')  # Per-worker, not global
def call_api(data):
    ...

# Dependencies? Task chains or callbacks
@app.task(bind=True)
def task_a(self):
    result = do_work()
    task_b.delay(result)  # Manually chain
```

---

class: center, middle

# runcue

## A Simpler Model

---

## The runcue Philosophy

```
Traditional:  "Task B depends on Task A completing"

runcue:       "Task B is ready when its inputs are valid"
```

**Artifacts are the source of truth, not task completion.**

```
┌─────────────────────────────────────────────────────────┐
│                    Your Application                      │
│                                                          │
│   is_ready(work)  → "Can this run? Are inputs valid?"   │
│   is_stale(work)  → "Should this run? Output valid?"    │
│   task handlers   → "Here's how to do the work"         │
│                                                          │
└────────────────────────▲─────────────────────────────────┘
                         │
                         │  "Ready?"  "Stale?"  "Go!"
                         │
┌────────────────────────┴─────────────────────────────────┐
│                        runcue                            │
│                                                          │
│   Queue → Check Readiness → Respect Limits → Execute    │
│                                                          │
│            NO PERSISTENCE · NO EXTERNAL SERVICES         │
└──────────────────────────────────────────────────────────┘
```

---

## What runcue Is

**An embedded coordinator for rate-limited work**

- ✅ Runs in your process (no external services)
- ✅ Fully in-memory (no Redis, no database)
- ✅ Stateless (your artifacts are the truth)
- ✅ Lightweight (just Python)

**runcue handles the WHEN. You handle the WHAT.**

```python
import runcue

cue = runcue.Cue()
cue.service("openai", rate="60/min", concurrent=5)

@cue.task("extract_text", uses="openai")
def extract_text(work):
    # You decide what happens
    text = call_openai(work.params["image"])
    Path(work.params["output"]).write_text(text)
    return {"done": True}
```

---

## The Two Questions

runcue asks your code two questions for each work unit:

| Question | Callback | Purpose |
|----------|----------|---------|
| **"Can this start?"** | `is_ready` | Check if inputs exist/valid |
| **"Should this run?"** | `is_stale` | Check if output is missing/outdated |

```python
@cue.is_ready
def is_ready(work) -> bool:
    """Consumer check: are my inputs valid?"""
    if work.task == "summarize":
        return Path(work.params["text_path"]).exists()
    return True

@cue.is_stale  
def is_stale(work) -> bool:
    """Producer check: is my output invalid?"""
    if work.task == "extract_text":
        output = work.params["image"].replace(".png", ".txt")
        return not Path(output).exists()
    return True  # Default: run if ready
```

---

## Services: Rate Limit Buckets

Services don't execute anything—they're buckets that track usage:

```python
# 60 requests per minute, max 5 concurrent
cue.service("openai", rate="60/min", concurrent=5)

# No rate limit, 4 parallel workers
cue.service("local", concurrent=4)

# Serial access only
cue.service("knife", concurrent=1)

# Rate limited, any concurrency
cue.service("email", rate="100/hour")
```

runcue ensures limits aren't exceeded before calling your handlers.

---

class: center, middle

# Power in Practice

---

## Example: PDF Processing

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
```

---

## Example: Readiness & Staleness

```python
@cue.is_ready
def is_ready(work):
    if work.task == "extract_text":
        # Wait for the page image to exist
        return Path(work.params["page_path"]).exists()
    return True

@cue.is_stale
def is_stale(work):
    if work.task == "extract_text":
        # Run if text file doesn't exist
        output = work.params["page_path"].replace(".png", ".txt")
        return not Path(output).exists()
    return True
```

**What this enables:**

```python
# Re-process just pages 5-10
for i in range(5, 11):
    await cue.submit("extract_text", params={
        "page_path": f"work/{job_id}/pages/{i:03d}.png"
    })
# Pages exist → runs immediately. No need to re-split!
```

---

## The Artifact Pattern

Every task is a **producer** (creates output) and often a **consumer** (needs input):

```
┌──────────────┐         ┌──────────────┐         ┌──────────────┐
│  split_pdf   │────────▶│ extract_text │────────▶│  summarize   │
│  (producer)  │  pages  │    (both)    │  text   │  (consumer)  │
└──────────────┘         └──────────────┘         └──────────────┘
```

**Define validation once, use everywhere:**

```python
def text_is_valid(path: str) -> bool:
    """Is this text artifact valid and fresh?"""
    p = Path(path)
    if not p.exists():
        return False
    age = time.time() - p.stat().st_mtime
    return age < 86400  # Fresh if < 24 hours

# Use in is_ready (consumer) and is_stale (producer)
```

---

## Side Effects Need Proof

For tasks with side effects (email, payments), create proof artifacts:

```python
@cue.task("send_notification", uses="email")
def send_notification(work):
    order_id = work.params["order_id"]
    proof_path = f"proof/{order_id}/notification.json"
    
    # Idempotent: check proof first
    if Path(proof_path).exists():
        return json.loads(Path(proof_path).read_text())
    
    # Do the side effect
    result = send_email(to=work.params["email"], 
                        subject=f"Order {order_id} shipped")
    
    # Create proof artifact
    Path(proof_path).write_text(json.dumps({
        "sent_at": time.time(),
        "message_id": result.message_id
    }))
    return {"message_id": result.message_id}
```

**Now resubmitting won't send duplicate emails.**

---

## Callbacks for Observability

runcue doesn't store history—you do, however you want:

```python
@cue.on_complete
def on_complete(work, result, duration):
    logging.info(f"{work.task} completed in {duration:.2f}s")
    my_metrics.histogram("task_duration", duration)
    my_database.log(work.id, result)

@cue.on_failure
def on_failure(work, error):
    logging.error(f"{work.task} failed: {error}")
    slack.post(f"Failed: {work.task} - {error}")

@cue.on_skip
def on_skip(work):
    # Called when is_stale returned False (output valid)
    logging.debug(f"Skipped {work.task} - already done")

@cue.on_start
def on_start(work):
    metrics.increment("started", task=work.task)
```

---

class: center, middle

# The Comparison

---

## Scenario: Re-run One Step

| Approach | What Happens |
|----------|--------------|
| **Traditional** | Re-run entire chain, or complex logic to skip steps |
| **runcue** | Just submit it. If inputs exist, it runs immediately. |

```python
# Traditional (Celery): awkward
@app.task
def extract_text(page_id):
    output = get_output_path(page_id)
    if os.path.exists(output):  # Manual check
        return {"skipped": True}
    # ... actual work

# runcue: natural
await cue.submit("extract_text", params={"page_path": "page5.png"})
# is_ready: does page5.png exist? Yes → run
# is_stale: does page5.txt exist? No → run
```

---

## Scenario: Inputs Go Stale

| Approach | What Happens |
|----------|--------------|
| **Traditional** | Manual intervention required |
| **runcue** | `is_ready` blocks downstream, `is_stale` triggers upstream |

```python
# If slices go stale after 24 hours:

@cue.is_ready
def is_ready(work):
    if work.task == "make_pie":
        return slices_are_fresh(work.params["job_id"])
    return True

@cue.is_stale
def is_stale(work):
    if work.task == "slice_apples":
        return not slices_are_fresh(work.params["job_id"])
    return True

# Automatic: stale slices → make_pie waits → slice_apples re-runs
```

---

## Scenario: Crash Recovery

| Approach | What Happens |
|----------|--------------|
| **Traditional** | Check broker state, replay from checkpoint |
| **runcue** | Resubmit everything. `is_stale` skips what's done. |

```python
# After crash, just resubmit all work:
cue.start()
for doc in documents:
    await cue.submit("process", params={"path": doc})

# is_stale checks each artifact:
# - Output exists and fresh? Skip (on_skip fires)
# - Output missing or stale? Run
```

**No checkpoint files. No "where were we?" logic.**

---

## Setup Comparison

**Traditional (Celery + Redis):**
```bash
# Install
pip install celery redis

# Run Redis
docker run -d -p 6379:6379 redis

# Configure (settings.py - 15+ lines)
# Define tasks (with manual rate limiting)
# Run worker: celery -A myapp worker
# Run beat (for scheduling): celery -A myapp beat
# Set up monitoring (Flower): celery -A myapp flower
```

**runcue:**
```bash
pip install runcue
```

```python
cue = runcue.Cue()
cue.service("api", rate="60/min", concurrent=5)
cue.start()
```

---

## When to Use What

**Use runcue when:**
- CLI tools and batch processors
- Scripts coordinating multiple APIs
- Local applications with subprocesses
- Continuous background workers
- You want artifact-based dependencies

**Use traditional queues when:**
- Distributed systems (work across machines)
- Durable queues (must survive restart)
- Very high throughput (>10k tasks/minute)
- Request-based web applications

---

class: center, middle

# Summary

---

## The runcue Philosophy

1. **Artifacts are proof** — Every task produces verifiable evidence
2. **Validation is centralized** — Define once, use in `is_ready` and `is_stale`
3. **runcue is stateless** — It asks "ready?" and "stale?"—you answer
4. **You own your data** — History, metrics, caching via callbacks

```
Your App                       runcue                    Your Handlers
    │                            │                            │
    │  submit(task, params)      │                            │
    │ ─────────────────────────► │                            │
    │                            │  is_ready? is_stale?       │
    │  ◄─────────────────────────│  (your callbacks)          │
    │                            │                            │
    │                            │  handler(work)             │
    │                            │ ──────────────────────────►│
    │                            │        result              │
    │                            │ ◄──────────────────────────│
    │  on_complete callback      │                            │
    │ ◄───────────────────────── │                            │
```

---

## Key Takeaways

| Traditional | runcue |
|-------------|--------|
| Task chains based on completion | Dependencies based on artifact validity |
| External broker required | Embedded, in-memory |
| Persistence in broker | Artifacts are the truth |
| Rate limiting per-worker | Global rate limiting |
| Complex retry configuration | You decide via callbacks |
| Setup: broker + workers + monitoring | `pip install runcue` |

---

class: center, middle

# Questions?

**GitHub:** github.com/somebox/runcue

```bash
pip install runcue
```

---

## Bonus: Quick Start

```python
import runcue
from pathlib import Path

cue = runcue.Cue()
cue.service("api", rate="60/min", concurrent=5)

@cue.task("process", uses="api")
def process(work):
    # Your logic here
    output = do_work(work.params["input"])
    Path(work.params["output"]).write_text(output)
    return {"done": True}

@cue.is_ready
def is_ready(work):
    return Path(work.params["input"]).exists()

@cue.is_stale
def is_stale(work):
    return not Path(work.params["output"]).exists()

async def main():
    cue.start()
    await cue.submit("process", params={
        "input": "data.txt",
        "output": "result.txt"
    })
    await cue.stop()
```

