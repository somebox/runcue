# runcue-sim

A CLI tool for testing runcue behavior under various conditions without real services.

## Quick Start

```bash
# Basic run
uv run runcue-sim --count 100 --latency 200

# With rate limiting
uv run runcue-sim --count 200 --latency 100 --concurrent 5 --rate-limit 30/60

# With errors and outliers
uv run runcue-sim --count 100 --latency 100 --error-rate 0.1 --outliers 0.05
```

## CLI Options

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

## TUI Display

When Rich is available, shows a live dashboard:

```
╭─────────────────────────── runcue-sim ───────────────────────────╮
│ ╭───────────────────────────── Queue ──────────────────────────╮ │
│ │ Queued: 847    Running: 5    Completed: 1,203   Failed: 23   │ │
│ │ Backpressure: OFF      Progress: 85%     Throughput: 45.2/s  │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
│ ╭──────────────────────────── Services ────────────────────────╮ │
│ │ mock_api    ████░░░░░░ 4/5 concurrent    52/60    ● OK       │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
│ ╭─────────────────────────── Recent Events ────────────────────╮ │
│ │ 12:34:56  completed   w_847     mock_work   245ms            │ │
│ │ 12:34:55  failed      w_832     mock_work   timeout          │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
│ ╭───────────────────────────── Config ─────────────────────────╮ │
│ │ Latency: 100ms ±20%  Outliers: 5%  Error: 10%  Target: 1000  │ │
│ ╰──────────────────────────────────────────────────────────────╯ │
╰──────────────────────────────────────────────────────────────────╯
```

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
```

## Profiles (Planned)

YAML-based profiles for repeatable scenarios:

```yaml
# profiles/stress.yaml
name: stress
description: High load stress test
config:
  count: 1000
  latency_ms: 100
  jitter: 0.3
  outliers: 0.05
  error_rate: 0.1
  concurrent: 10
  rate_limit: [100, 60]
```

```bash
# Run a profile
runcue-sim --profile stress

# Override profile settings
runcue-sim --profile stress --count 500

# List available profiles
runcue-sim --list-profiles
```

## How It Works

The simulator creates a real `runcue.Cue` instance with a mock task handler that simulates latency and errors. Everything runs in-memory—no persistence, no artifacts.

```
src/runcue_sim/
├── cli.py      # Argument parsing, main entry point
├── runner.py   # SimConfig, SimulationRunner - orchestrates simulation
└── display.py  # SimulationState, SimulatorDisplay - TUI rendering
```

- `SimulationRunner` creates a `runcue.Cue` instance with a mock task
- `SimulationState` is a dataclass updated by the runner, rendered by display
- The mock handler simulates latency/errors without external dependencies
- Rate limits and concurrency are tested against runcue's in-memory tracking
