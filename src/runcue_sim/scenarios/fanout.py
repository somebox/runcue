"""Fanout scenario - split → process in parallel → aggregate.

Demonstrates dependency chains using is_ready callback.
One split produces N items that are processed in parallel,
then aggregated back into a single result.
"""

from __future__ import annotations

import asyncio
import random
import time
from typing import TYPE_CHECKING

from runcue_sim.scenarios import Scenario, ScenarioInfo

if TYPE_CHECKING:
    import runcue
    from runcue_sim.display import SimulationState
    from runcue_sim.runner import SimConfig


class FanoutScenario(Scenario):
    """Split → process in parallel → aggregate.
    
    Each split job produces multiple process jobs.
    Process jobs run in parallel with rate limiting.
    An aggregate job waits for all process jobs to complete.
    
    Demonstrates:
    - is_ready for dependency gating
    - Dynamic work submission from handlers
    - Parallel processing with limits
    """
    
    FANOUT_SIZE = 5  # Each split produces this many items
    
    @property
    def info(self) -> ScenarioInfo:
        return ScenarioInfo(
            name="fanout",
            description="Split → process in parallel → aggregate",
        )
    
    def setup(self, cue: runcue.Cue, config: SimConfig, state: SimulationState) -> None:
        """Configure splitter, processor, and aggregator services."""
        from runcue_sim.display import ServiceStatus
        
        # Track artifacts for dependency checking
        artifacts: dict[str, set[int]] = {}  # batch_id -> set of completed indices
        batch_sizes: dict[str, int] = {}  # batch_id -> total items
        
        # Build rate string if specified
        rate_str = None
        if config.rate_limit:
            count, seconds = config.rate_limit
            if seconds == 1:
                rate_str = f"{count}/sec"
            elif seconds == 60:
                rate_str = f"{count}/min"
            else:
                rate_str = f"{int(count * 60 / seconds)}/min"
        
        # Register services
        cue.service("splitter", concurrent=1)  # Serial splitting
        cue.service("processor", concurrent=config.max_concurrent, rate=rate_str)
        cue.service("aggregator", concurrent=2)
        
        # Update service display
        now = time.time()
        state.services["splitter"] = ServiceStatus(name="splitter", max_concurrent=1, start_time=now)
        state.services["processor"] = ServiceStatus(
            name="processor",
            max_concurrent=config.max_concurrent,
            rate_limit=config.rate_limit[0] if config.rate_limit else None,
            rate_window=config.rate_limit[1] if config.rate_limit else None,
            start_time=now,
        )
        state.services["aggregator"] = ServiceStatus(name="aggregator", max_concurrent=2, start_time=now)
        
        # Store cue reference for dynamic submission
        self._cue = cue
        self._state = state
        self._config = config
        self._artifacts = artifacts
        self._batch_sizes = batch_sizes
        
        # --- Split task ---
        @cue.task("split", uses="splitter")
        async def split_handler(work):
            batch_id = work.params["batch_id"]
            
            # Simulate splitting work
            await asyncio.sleep(0.05)  # Quick split
            
            # Initialize artifact tracking
            artifacts[batch_id] = set()
            batch_sizes[batch_id] = self.FANOUT_SIZE
            
            # Submit process jobs
            for i in range(self.FANOUT_SIZE):
                await cue.submit("process", params={
                    "batch_id": batch_id,
                    "index": i,
                })
                state.submitted += 1
                state.queued += 1
            
            # Submit aggregate job (will wait for process jobs)
            await cue.submit("aggregate", params={"batch_id": batch_id})
            state.submitted += 1
            state.queued += 1
            
            return {"items": self.FANOUT_SIZE}
        
        # --- Process task ---
        @cue.task("process", uses="processor")
        async def process_handler(work):
            batch_id = work.params["batch_id"]
            index = work.params["index"]
            
            # Simulate processing with latency
            base_latency = config.latency_ms / 1000.0
            if base_latency > 0:
                jitter = config.latency_jitter
                actual_latency = base_latency * random.uniform(1 - jitter, 1 + jitter)
                await asyncio.sleep(actual_latency)
            
            # Simulate errors
            if random.random() < config.error_rate:
                raise RuntimeError("Simulated error")
            
            # Mark this item as complete
            if batch_id in artifacts:
                artifacts[batch_id].add(index)
            
            return {"batch_id": batch_id, "index": index}
        
        # --- Aggregate task ---
        @cue.task("aggregate", uses="aggregator")
        async def aggregate_handler(work):
            batch_id = work.params["batch_id"]
            
            # Simulate aggregation
            await asyncio.sleep(0.03)
            
            return {"batch_id": batch_id, "aggregated": True}
        
        # --- is_ready callback ---
        @cue.is_ready
        def is_ready(work):
            if work.task == "aggregate":
                batch_id = work.params.get("batch_id")
                if batch_id not in artifacts or batch_id not in batch_sizes:
                    return False
                # Ready when all items in batch are complete
                return len(artifacts[batch_id]) >= batch_sizes[batch_id]
            return True
        
        # --- Callbacks to track state ---
        @cue.on_start
        def on_start(work):
            state.running += 1
            if state.queued > 0:
                state.queued -= 1
            svc = state.services.get(work.task.split("_")[0] if "_" in work.task else 
                                     {"split": "splitter", "process": "processor", "aggregate": "aggregator"}.get(work.task, "processor"))
            if svc:
                svc.current_concurrent += 1
            state.add_event("started", work.id, work.task, "")
        
        @cue.on_complete
        def on_complete(work, result, duration):
            state.running = max(0, state.running - 1)
            state.completed += 1
            task_to_svc = {"split": "splitter", "process": "processor", "aggregate": "aggregator"}
            svc = state.services.get(task_to_svc.get(work.task, "processor"))
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_completed += 1
            state.add_event("completed", work.id, work.task, f"{int(duration*1000)}ms")
        
        @cue.on_failure
        def on_failure(work, error):
            state.running = max(0, state.running - 1)
            state.failed += 1
            task_to_svc = {"split": "splitter", "process": "processor", "aggregate": "aggregator"}
            svc = state.services.get(task_to_svc.get(work.task, "processor"))
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_failed += 1
            state.add_event("failed", work.id, work.task, str(error))
    
    async def submit_workload(self, cue: runcue.Cue, config: SimConfig, state: SimulationState) -> None:
        """Submit initial split jobs.
        
        Each split job will fan out into process jobs.
        Total work: count splits × FANOUT_SIZE processes × 1 aggregate each.
        """
        for i in range(config.count):
            await cue.submit("split", params={"batch_id": f"batch_{i:04d}"})
            state.submitted += 1
            state.queued += 1
            state.add_event("queued", f"split_{i}", "split", f"batch_{i:04d}")
            
            if config.submit_rate:
                await asyncio.sleep(1.0 / config.submit_rate)

