# runcue-sim

A CLI tool for testing runcue behavior under various conditions without real services.

## Quick Start

```bash
# Basic run with default scenario
uv run runcue-sim --count 100 --latency 200

# Run a specific scenario
uv run runcue-sim --scenario fanout

# With rate limiting
uv run runcue-sim --count 200 --latency 100 --concurrent 5 --rate-limit 30/60

# With errors and outliers
uv run runcue-sim --count 100 --latency 100 --error-rate 0.1 --outliers 0.05
```

## Scenarios vs Profiles

The simulator supports two complementary concepts:

| Concept | Purpose | Example |
|---------|---------|---------|
| **Scenarios** | Define topology and behavior | Fan-out pipeline, flaky network |
| **Profiles** | Tune parameters | Stress test, slow API simulation |

**Scenarios** define *what* is being simulated (services, tasks, dependencies).
**Profiles** tune *how* it behaves (latency, error rates, concurrency).

---

## Scenarios

Scenarios define mock service topologies and task behaviors for testing different workload patterns.

### Built-in Scenarios

| Scenario | Description |
|----------|-------------|
| `single_queue` | Single service, independent tasks (default) |
| `fanout` | Split → process in parallel → aggregate |
| `pipeline` | A → B → C chain with different services |
| `flaky_network` | High error rate, timeouts, variable latency |
| `rate_battle` | Multiple tasks competing for rate-limited service |
| `mixed_services` | Fast local + slow API tasks |

### CLI Usage

```bash
# List available scenarios
runcue-sim --list-scenarios

# Run a built-in scenario
runcue-sim --scenario fanout

# Override scenario settings
runcue-sim --scenario fanout --count 20 --latency 300

# Custom scenario file
runcue-sim --scenario-file ./my_scenario.yaml
```

### Scenario Definition Format

Scenarios are defined in YAML:

```yaml
# scenarios/fanout.yaml
name: fanout
description: Split work then process pages in parallel

services:
  splitter:
    concurrent: 1
  processor:
    concurrent: 5
    rate: 60/min
  aggregator:
    concurrent: 1

tasks:
  split:
    uses: splitter
    latency: 50ms
    produces: 10          # Each split produces 10 downstream items
    
  process:
    uses: processor
    latency: 200ms
    jitter: 0.3
    depends_on: split     # Waits for split to complete
    
  aggregate:
    uses: aggregator
    latency: 100ms
    depends_on: process   # Waits for all process tasks

workload:
  initial:
    task: split
    count: 5              # 5 splits → 50 processes → 5 aggregates
```

### Scenario Examples

#### Single Queue (Default)

```yaml
name: single_queue
description: Basic throughput test, no dependencies

services:
  api:
    concurrent: 5
    rate: 60/min

tasks:
  work:
    uses: api
    latency: 100ms
    jitter: 0.2

workload:
  initial:
    task: work
    count: 100
```

#### Flaky Network

```yaml
name: flaky_network
description: Unreliable external service simulation

services:
  external:
    concurrent: 3
    rate: 30/min

tasks:
  call_api:
    uses: external
    latency: 300ms
    jitter: 0.5
    error_rate: 0.25
    error_types:
      timeout: 0.4        # 40% of errors are timeouts
      rate_limit: 0.3     # 30% are 429s
      server_error: 0.3   # 30% are 5xx

workload:
  initial:
    task: call_api
    count: 50
```

#### Pipeline

```yaml
name: pipeline
description: Sequential processing chain

services:
  local:
    concurrent: 4
  api:
    concurrent: 2
    rate: 30/min
  storage:
    concurrent: 2

tasks:
  extract:
    uses: local
    latency: 20ms
    
  transform:
    uses: api
    latency: 200ms
    depends_on: extract
    
  load:
    uses: storage
    latency: 50ms
    depends_on: transform

workload:
  initial:
    task: extract
    count: 20
```

#### Mixed Services

```yaml
name: mixed_services
description: Local processing with occasional API calls

services:
  local:
    concurrent: 8
  api:
    concurrent: 2
    rate: 20/min

tasks:
  fast_local:
    uses: local
    latency: 10ms
    weight: 0.8           # 80% of work
    
  slow_api:
    uses: api
    latency: 500ms
    jitter: 0.4
    weight: 0.2           # 20% of work

workload:
  initial:
    task: mixed           # Distributes by weight
    count: 100
```

---

## Profiles

Profiles provide parameter presets for common testing scenarios.

### CLI Usage

```bash
# List available profiles
runcue-sim --list-profiles

# Run with a profile
runcue-sim --profile stress

# Combine scenario and profile
runcue-sim --scenario fanout --profile slow_api

# Override profile settings
runcue-sim --profile stress --count 500
```

### Built-in Profiles

| Profile | Description |
|---------|-------------|
| `default` | Balanced settings for general testing |
| `stress` | High volume, fast handlers |
| `slow_api` | High latency, variable response times |
| `error_prone` | High error rate for failure handling tests |
| `rate_limited` | Tight rate limits to test throttling |

### Profile Definition

```yaml
# profiles/stress.yaml
name: stress
description: High load stress test
config:
  count: 1000
  latency_ms: 50
  jitter: 0.2
  outliers: 0.02
  error_rate: 0.05
  concurrent: 10
  rate_limit: [100, 60]   # 100 per 60 seconds
```

```yaml
# profiles/slow_api.yaml
name: slow_api
description: Simulates slow external API
config:
  latency_ms: 500
  jitter: 0.5
  outliers: 0.1
  outlier_mult: 3.0
  concurrent: 3
  rate_limit: [30, 60]
```

---

## CLI Options

### Scenario & Profile Selection

| Option | Description |
|--------|-------------|
| `--scenario` | Built-in scenario name |
| `--scenario-file` | Path to custom scenario YAML |
| `--profile` | Built-in profile name |
| `--profile-file` | Path to custom profile YAML |
| `--list-scenarios` | List available scenarios |
| `--list-profiles` | List available profiles |

### Work Configuration

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--count` | `-n` | 100 | Number of work units to submit |
| `--duration` | `-d` | — | Max runtime in seconds (stops early if set) |
| `--submit-rate` | `-s` | — | Work/second submission rate (default: batch all) |

### Latency Simulation

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--latency` | `-l` | 100 | Base handler latency in ms |
| `--jitter` | `-j` | 0.2 | Variance as fraction (0.2 = ±20%) |
| `--outliers` | — | 0.0 | Probability of outlier request (0.0-1.0) |
| `--outlier-mult` | — | 5.0 | Outlier latency multiplier |

**How latency works:**
- Normal requests: `base_latency * uniform(1-jitter, 1+jitter)`
- Outliers: `base_latency * outlier_mult * uniform(0.8, 1.5)`

Example: `--latency 100 --jitter 0.3 --outliers 0.1` means:
- 90% of requests: 70-130ms
- 10% of requests: 400-750ms (outliers)

### Service Limits

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--concurrent` | `-c` | 5 | Max concurrent work |
| `--rate-limit` | `-r` | — | Rate limit as `requests/seconds` |
| `--error-rate` | `-e` | 0.0 | Fraction of work that fails (0.0-1.0) |

### Display

| Option | Default | Description |
|--------|---------|-------------|
| `--tui` | auto | Force Rich TUI display |
| `--no-tui` | — | Simple text output |
| `--verbose` | — | Show library debug logs |

---

## TUI Display

When Rich is available, shows a live dashboard:

```
╭─────────────────────────── runcue-sim ───────────────────────────╮
│ Scenario: fanout                    Profile: default             │
│ ╭───────────────────────────── Queue ──────────────────────────╮ │
│ │ Queued: 847    Running: 5    Completed: 1,203   Failed: 23   │ │
│ │ Backpressure: OFF      Progress: 85%     Throughput: 45.2/s  │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
│ ╭──────────────────────────── Services ────────────────────────╮ │
│ │ splitter    █░░░░░░░░░ 1/1 concurrent    12/∞     ● OK       │ │
│ │ processor   ████░░░░░░ 4/5 concurrent    52/60    ● OK       │ │
│ │ aggregator  █░░░░░░░░░ 1/1 concurrent     8/∞     ● OK       │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
│ ╭─────────────────────────── Recent Events ────────────────────╮ │
│ │ 12:34:56  completed   w_847     process     245ms            │ │
│ │ 12:34:55  failed      w_832     process     timeout          │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
│ ╭───────────────────────────── Config ─────────────────────────╮ │
│ │ Latency: 100ms ±20%  Outliers: 5%  Error: 10%  Target: 1000  │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
╰──────────────────────────────────────────────────────────────────╯
```

---

## Examples

```bash
# Test concurrency limits
uv run runcue-sim -n 50 -l 500 -c 2
# Expect: max 2 running at once, ~25s total

# Test rate limiting
uv run runcue-sim -n 100 -l 10 -r 20/1
# Expect: ~20/s throughput despite fast handlers

# Stress test with failures
uv run runcue-sim -n 500 -l 50 -c 10 -e 0.2
# Expect: ~20% failures, ~100 failed

# Realistic API simulation
uv run runcue-sim -n 200 -l 150 --jitter 0.4 --outliers 0.03 -c 5 -r 60/60
# Expect: variable latency, occasional slow requests, rate limited

# Fan-out scenario
uv run runcue-sim --scenario fanout --count 10
# Expect: 10 splits → 100 processes → 10 aggregates

# Flaky network with stress profile
uv run runcue-sim --scenario flaky_network --profile stress
# Expect: high volume with frequent failures
```

---

## How It Works

The simulator creates a real `runcue.Cue` instance with mock task handlers that simulate latency and errors. Everything runs in-memory—no persistence, no artifacts.

```
src/runcue_sim/
├── cli.py           # Argument parsing, main entry point
├── runner.py        # SimConfig, SimulationRunner - orchestrates simulation
├── display.py       # SimulationState, SimulatorDisplay - TUI rendering
└── scenarios/       # Built-in scenario definitions
    ├── __init__.py
    ├── single_queue.py
    ├── fanout.py
    ├── pipeline.py
    └── flaky_network.py
```

- `SimulationRunner` creates a `runcue.Cue` instance with mock tasks
- Scenarios define services, tasks, and their relationships
- Profiles provide parameter presets
- The mock handlers simulate latency/errors without external dependencies
- Rate limits and concurrency are tested against runcue's in-memory tracking

### Scenario Implementation

Scenarios register services and tasks dynamically:

```python
class FanoutScenario:
    def setup(self, cue: Cue, config: SimConfig):
        cue.service("splitter", concurrent=1)
        cue.service("processor", concurrent=config.concurrent, rate=config.rate)
        cue.service("aggregator", concurrent=1)
        
        @cue.task("split", uses="splitter")
        async def split(work):
            await simulate_latency(50)
            # Queue downstream work
            for i in range(10):
                await cue.submit("process", params={"parent": work.id, "index": i})
            return {"items": 10}
        
        @cue.task("process", uses="processor")
        async def process(work):
            await simulate_latency(config.latency, config.jitter)
            return {}
        
        # ... etc
```

---

## Future Ideas

- **Shell command scenarios**: Test subprocess-based tasks
- **Artifact simulation**: Mock file system for is_ready/is_stale testing
- **Network simulation**: Configurable latency distributions (normal, bimodal)
- **Chaos mode**: Random service failures, network partitions
- **Metrics export**: Prometheus/StatsD output for dashboards
