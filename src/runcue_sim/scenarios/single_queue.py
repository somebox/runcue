"""Single queue scenario - the default workload pattern.

Simple throughput test with a single service and independent tasks.
No dependencies between work items.
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


class SingleQueueScenario(Scenario):
    """Single service, independent tasks.
    
    The simplest scenario - all work goes through one service with
    configurable rate limits and concurrency. No task dependencies.
    """
    
    @property
    def info(self) -> ScenarioInfo:
        return ScenarioInfo(
            name="single_queue",
            description="Single service, independent tasks (default)",
        )
    
    def setup(self, cue: runcue.Cue, config: SimConfig, state: SimulationState) -> None:
        """Configure a single service and task."""
        from runcue_sim.display import ServiceStatus
        
        # Build rate string if specified
        rate_str = None
        if config.rate_limit:
            count, seconds = config.rate_limit
            if seconds == 1:
                rate_str = f"{count}/sec"
            elif seconds == 60:
                rate_str = f"{count}/min"
            elif seconds == 3600:
                rate_str = f"{count}/hour"
            else:
                rate_str = f"{int(count * 60 / seconds)}/min"
        
        # Register service
        cue.service("api", concurrent=config.max_concurrent, rate=rate_str)
        
        # Update service display
        state.services["api"] = ServiceStatus(
            name="api",
            max_concurrent=config.max_concurrent,
            rate_limit=config.rate_limit[0] if config.rate_limit else None,
            rate_window=config.rate_limit[1] if config.rate_limit else None,
            start_time=time.time(),
        )
        
        # Track work timing
        work_started: dict[str, float] = {}
        
        # Register task
        @cue.task("work", uses="api")
        async def work_handler(work):
            work_id = work.id
            work_started[work_id] = time.time()
            
            try:
                # Calculate latency with jitter and possible outliers
                base_latency = config.latency_ms / 1000.0
                actual_latency = base_latency
                is_outlier = False
                
                if base_latency > 0:
                    if config.outlier_chance > 0 and random.random() < config.outlier_chance:
                        actual_latency = base_latency * config.outlier_multiplier
                        actual_latency *= random.uniform(0.8, 1.5)
                        is_outlier = True
                    else:
                        jitter = config.latency_jitter
                        actual_latency = base_latency * random.uniform(1 - jitter, 1 + jitter)
                    
                    await asyncio.sleep(actual_latency)
                
                # Simulate errors
                if random.random() < config.error_rate:
                    raise RuntimeError("Simulated error")
                
                duration_ms = int((time.time() - work_started.get(work_id, time.time())) * 1000)
                return {"latency_ms": duration_ms, "outlier": is_outlier}
                
            finally:
                work_started.pop(work_id, None)
        
        # Register callbacks to track state
        @cue.on_start
        def on_start(work):
            state.running += 1
            if state.queued > 0:
                state.queued -= 1
            svc = state.services.get("api")
            if svc:
                svc.current_concurrent += 1
            state.add_event("started", work.id, "work", work.params.get("item", ""))
        
        @cue.on_complete
        def on_complete(work, result, duration):
            state.running = max(0, state.running - 1)
            state.completed += 1
            svc = state.services.get("api")
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_completed += 1
            
            duration_ms = int(duration * 1000)
            detail = f"{duration_ms}ms"
            if result and result.get("outlier"):
                detail += " [outlier]"
            state.add_event("completed", work.id, "work", detail)
        
        @cue.on_failure
        def on_failure(work, error):
            state.running = max(0, state.running - 1)
            state.failed += 1
            svc = state.services.get("api")
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_failed += 1
            state.add_event("failed", work.id, "work", str(error))
    
    async def submit_workload(self, cue: runcue.Cue, config: SimConfig, state: SimulationState) -> None:
        """Submit independent work items."""
        for i in range(config.count):
            await cue.submit("work", params={"item": f"item_{i:04d}", "index": i})
            state.submitted += 1
            state.queued += 1
            state.add_event("queued", f"work_{i}", "work", f"item_{i:04d}")
            
            # Rate-limited submission
            if config.submit_rate:
                await asyncio.sleep(1.0 / config.submit_rate)

