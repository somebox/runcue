# runcue-sim

A CLI tool for testing runcue behavior interactively. Runs simulations with mock tasks that respect rate limits, concurrency, and dependencies—without calling real APIs.

## Quick Start

```bash
# Basic run with default settings
uv run runcue-sim --count 50 --latency 100

# With rate limiting
uv run runcue-sim --count 100 --latency 50 --rate-limit 20/1 --concurrent 5

# With errors and slow outliers
uv run runcue-sim --count 100 --latency 100 --error-rate 0.1 --outliers 0.05

# Run a specific scenario
uv run runcue-sim --scenario fanout --count 5
uv run runcue-sim --scenario pipeline --count 10

# List available scenarios
uv run runcue-sim --list-scenarios
```

---

## How to Use It

### Basic Usage

The simulator creates mock work items that simulate API latency and errors:

```bash
uv run runcue-sim --count 100 --latency 50
```

This submits 100 work items, each taking ~50ms (with ±20% jitter by default).

### Testing Rate Limits

```bash
# 20 requests per second, 5 concurrent
uv run runcue-sim --count 100 --latency 10 --rate-limit 20/1 --concurrent 5
```

You'll see work throttled to respect the rate limit. The TUI shows:
- Current concurrent count vs limit
- Items processed per service
- Overall throughput

### Testing Error Handling

```bash
# 10% of requests fail
uv run runcue-sim --count 50 --latency 100 --error-rate 0.1
```

Failed work appears in red in the event log. Per-service stats show `completed/failed` counts.

### Testing Latency Variance

```bash
# High jitter (±50%) with 5% outliers that take 5x longer
uv run runcue-sim --count 100 --latency 100 --jitter 0.5 --outliers 0.05
```

Outliers simulate slow API responses and are marked `[outlier]` in events.

---

## Scenarios

Scenarios define workload patterns—services, tasks, and dependencies.

### Available Scenarios

| Scenario | Description |
|----------|-------------|
| `single_queue` | Single service, independent tasks (default) |
| `fanout` | Split → process in parallel → aggregate |
| `pipeline` | Extract → Transform → Load chain |

### single_queue (Default)

All work goes through one service with configurable rate limits. No dependencies between items.

```bash
uv run runcue-sim --count 100 --latency 50
```

### fanout

Demonstrates producer/consumer patterns with `is_ready`:

1. **Split** (serial): Creates N process jobs
2. **Process** (parallel, rate-limited): Processes each item
3. **Aggregate** (waits): Runs after all process jobs complete

```bash
uv run runcue-sim --scenario fanout --count 5 --latency 50
# 5 splits → 25 processes → 5 aggregates = 35 total items
```

### pipeline

Three-stage ETL pipeline with sequential dependencies:

1. **Extract** (fast, local): Quick local processing
2. **Transform** (rate-limited): Main API work
3. **Load** (storage): Write results

```bash
uv run runcue-sim --scenario pipeline --count 10 --latency 100
# 10 extracts → 10 transforms → 10 loads = 30 total items
```

---

## CLI Options

### Work Configuration

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--count` | `-n` | 100 | Number of initial work units |
| `--duration` | `-d` | — | Max runtime in seconds |
| `--submit-rate` | `-s` | — | Work/second submission rate |

### Latency Simulation

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--latency` | `-l` | 100 | Base handler latency in ms |
| `--jitter` | `-j` | 0.2 | Variance as fraction (±20%) |
| `--outliers` | — | 0.0 | Chance of slow request (0.0-1.0) |
| `--outlier-mult` | — | 5.0 | Outlier latency multiplier |

### Service Limits

| Option | Short | Default | Description |
|--------|-------|---------|-------------|
| `--concurrent` | `-c` | 5 | Max concurrent work |
| `--rate-limit` | `-r` | — | Rate limit as `count/seconds` |
| `--error-rate` | `-e` | 0.0 | Fraction that fails (0.0-1.0) |

### Scenario Selection

| Option | Description |
|--------|-------------|
| `--scenario` | Scenario name (default: single_queue) |
| `--list-scenarios` | List available scenarios |

### Display

| Option | Description |
|--------|-------------|
| `--tui` | Force Rich TUI display |
| `--no-tui` | Simple text output |
| `--verbose` | Show debug logs |

---

## TUI Display

When Rich is available, the simulator shows a live dashboard:

```
╭───────────────────────────────── runcue-sim ─────────────────────────────────╮
│ ╭───────────────────────────────── Queue ──────────────────────────────────╮ │
│ │ Queued: 12       Running: 5         Completed: 83          Failed: 0     │ │
│ │ Backpressure: OFF         Progress: 83%        Throughput: 45.2/s        │ │
│ ╰──────────────────────────────────────────────────────────────────────────╯ │
│ ╭──────────────────────────────── Services ────────────────────────────────╮ │
│ │  splitter       █░░░░░░░ 1/1                     5      12.3/s     ●     │ │
│ │  processor      ████░░░░ 4/5                    52      38.1/s     ●     │ │
│ │  aggregator     ░░░░░░░░ 0/2                     3       8.2/s     ●     │ │
│ ╰──────────────────────────────────────────────────────────────────────────╯ │
│ ╭───────────────────────────── Recent Events ──────────────────────────────╮ │
│ │  12:34:56     completed      a1b2c3d4    process       245ms             │ │
│ │  12:34:55     failed         e5f6g7h8    process       timeout           │ │
│ ╰──────────────────────────────────────────────────────────────────────────╯ │
│ ╭───────────────────────────────── Config ─────────────────────────────────╮ │
│ │ Latency: 100ms ±20%  Outliers: 5%  Error: 10%  Target: 100               │ │
│ ╰──────────────────────────────────────────────────────────────────────────╯ │
╰──────────────────────────────────────────────────────────────────────────────╯
```

**Services panel shows:**
- Concurrent progress bar (`current/max`)
- Processed count (or `completed/failed` if failures)
- Throughput per service
- Status indicator

---

## How It Works

### Architecture

```
src/runcue_sim/
├── cli.py           # Argument parsing, main entry point
├── runner.py        # SimConfig, SimulationRunner
├── display.py       # SimulationState, TUI rendering
└── scenarios/       # Workload pattern definitions
    ├── __init__.py  # Scenario base class, registry
    ├── single_queue.py
    ├── fanout.py
    └── pipeline.py
```

### Simulation Flow

1. **CLI** parses arguments into `SimConfig`
2. **SimulationRunner** creates a `runcue.Cue()` instance
3. **Scenario** registers services, tasks, and callbacks
4. Tasks are mock handlers that `await asyncio.sleep(latency)`
5. **Callbacks** (`on_start`, `on_complete`, `on_failure`) update `SimulationState`
6. **Display** polls state and renders the TUI

### Scenarios

Scenarios implement the `Scenario` base class:

```python
class Scenario(ABC):
    @property
    def info(self) -> ScenarioInfo:
        """Return scenario metadata."""
        ...
    
    def setup(self, cue: Cue, config: SimConfig, state: SimulationState) -> None:
        """Configure services, tasks, and callbacks."""
        ...
    
    async def submit_workload(self, cue: Cue, config: SimConfig, state: SimulationState) -> None:
        """Submit initial work items."""
        ...
```

To add a new scenario:
1. Create `scenarios/my_scenario.py` with a class extending `Scenario`
2. Register it in `scenarios/__init__.py`

### State Tracking

Since runcue is stateless, the simulator tracks state via callbacks:

```python
@cue.on_start
def on_start(work):
    state.running += 1
    state.queued -= 1

@cue.on_complete
def on_complete(work, result, duration):
    state.running -= 1
    state.completed += 1

@cue.on_failure
def on_failure(work, error):
    state.running -= 1
    state.failed += 1
```

This demonstrates how real applications would track metrics using runcue's callback system.

---

## Examples

```bash
# Test concurrency limits
uv run runcue-sim -n 50 -l 500 -c 2
# Expect: max 2 running at once, ~12.5s total

# Test rate limiting
uv run runcue-sim -n 100 -l 10 -r 20/1
# Expect: ~20/s throughput despite fast handlers

# Stress test with failures
uv run runcue-sim -n 500 -l 50 -c 10 -e 0.2
# Expect: ~20% failures, ~100 failed

# Realistic API simulation
uv run runcue-sim -n 200 -l 150 --jitter 0.4 --outliers 0.03 -c 5 -r 60/60
# Expect: variable latency, occasional slow requests, rate limited

# Fan-out workflow
uv run runcue-sim --scenario fanout --count 10
# Expect: 10 splits → 50 processes → 10 aggregates

# ETL pipeline
uv run runcue-sim --scenario pipeline --count 20 --error-rate 0.1
# Expect: failures in transform stage, downstream work blocked
```
