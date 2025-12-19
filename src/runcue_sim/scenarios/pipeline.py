"""Pipeline scenario - A → B → C sequential processing chain.

Demonstrates a linear dependency chain where each step
waits for the previous step to complete.
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


class PipelineScenario(Scenario):
    """Extract → Transform → Load pipeline.
    
    Three-stage pipeline where:
    - Extract: Fast local processing
    - Transform: Rate-limited API calls
    - Load: Storage writes
    
    Each item flows through all three stages sequentially.
    
    Demonstrates:
    - is_ready for sequential dependencies
    - Different services with different limits
    - Mixed fast/slow processing
    """
    
    @property
    def info(self) -> ScenarioInfo:
        return ScenarioInfo(
            name="pipeline",
            description="Extract → Transform → Load chain",
        )
    
    def setup(self, cue: "runcue.Cue", config: "SimConfig", state: "SimulationState") -> None:
        """Configure extract, transform, and load services."""
        from runcue_sim.display import ServiceStatus
        
        # Track which items have completed each stage
        extracted: set[str] = set()
        transformed: set[str] = set()
        
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
        
        # Register services with different characteristics
        cue.service("local", concurrent=config.max_concurrent * 2)  # Fast local
        cue.service("api", concurrent=config.max_concurrent, rate=rate_str)  # Rate-limited API
        cue.service("storage", concurrent=config.max_concurrent)  # Storage writes
        
        # Update service display
        now = time.time()
        state.services["local"] = ServiceStatus(name="local", max_concurrent=config.max_concurrent * 2, start_time=now)
        state.services["api"] = ServiceStatus(
            name="api",
            max_concurrent=config.max_concurrent,
            rate_limit=config.rate_limit[0] if config.rate_limit else None,
            rate_window=config.rate_limit[1] if config.rate_limit else None,
            start_time=now,
        )
        state.services["storage"] = ServiceStatus(name="storage", max_concurrent=config.max_concurrent, start_time=now)
        
        # Store references
        self._cue = cue
        self._state = state
        self._config = config
        
        # --- Extract task (fast, local) ---
        @cue.task("extract", uses="local")
        async def extract_handler(work):
            item_id = work.params["item_id"]
            
            # Fast local processing (10% of configured latency)
            base_latency = config.latency_ms / 1000.0 * 0.1
            if base_latency > 0:
                await asyncio.sleep(base_latency * random.uniform(0.8, 1.2))
            
            # Mark as extracted
            extracted.add(item_id)
            
            # Submit transform job
            await cue.submit("transform", params={"item_id": item_id})
            state.submitted += 1
            state.queued += 1
            
            return {"item_id": item_id, "stage": "extract"}
        
        # --- Transform task (rate-limited API) ---
        @cue.task("transform", uses="api")
        async def transform_handler(work):
            item_id = work.params["item_id"]
            
            # Main processing with configured latency
            base_latency = config.latency_ms / 1000.0
            if base_latency > 0:
                jitter = config.latency_jitter
                actual_latency = base_latency * random.uniform(1 - jitter, 1 + jitter)
                
                # Occasional outliers
                if config.outlier_chance > 0 and random.random() < config.outlier_chance:
                    actual_latency *= config.outlier_multiplier
                
                await asyncio.sleep(actual_latency)
            
            # Simulate errors
            if random.random() < config.error_rate:
                raise RuntimeError("Transform error")
            
            # Mark as transformed
            transformed.add(item_id)
            
            # Submit load job
            await cue.submit("load", params={"item_id": item_id})
            state.submitted += 1
            state.queued += 1
            
            return {"item_id": item_id, "stage": "transform"}
        
        # --- Load task (storage writes) ---
        @cue.task("load", uses="storage")
        async def load_handler(work):
            item_id = work.params["item_id"]
            
            # Storage write (30% of configured latency)
            base_latency = config.latency_ms / 1000.0 * 0.3
            if base_latency > 0:
                await asyncio.sleep(base_latency * random.uniform(0.8, 1.2))
            
            return {"item_id": item_id, "stage": "load"}
        
        # --- is_ready callback ---
        @cue.is_ready
        def is_ready(work):
            item_id = work.params.get("item_id")
            if work.task == "transform":
                return item_id in extracted
            if work.task == "load":
                return item_id in transformed
            return True
        
        # --- Callbacks to track state ---
        task_to_service = {"extract": "local", "transform": "api", "load": "storage"}
        
        @cue.on_start
        def on_start(work):
            state.running += 1
            if state.queued > 0:
                state.queued -= 1
            svc = state.services.get(task_to_service.get(work.task, "api"))
            if svc:
                svc.current_concurrent += 1
            state.add_event("started", work.id, work.task, work.params.get("item_id", ""))
        
        @cue.on_complete
        def on_complete(work, result, duration):
            state.running = max(0, state.running - 1)
            state.completed += 1
            svc = state.services.get(task_to_service.get(work.task, "api"))
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_completed += 1
            state.add_event("completed", work.id, work.task, f"{int(duration*1000)}ms")
        
        @cue.on_failure
        def on_failure(work, error):
            state.running = max(0, state.running - 1)
            state.failed += 1
            svc = state.services.get(task_to_service.get(work.task, "api"))
            if svc:
                svc.current_concurrent = max(0, svc.current_concurrent - 1)
                svc.total_failed += 1
            state.add_event("failed", work.id, work.task, str(error))
    
    async def submit_workload(self, cue: "runcue.Cue", config: "SimConfig", state: "SimulationState") -> None:
        """Submit initial extract jobs.
        
        Each item will flow: extract → transform → load.
        Total work: count × 3 stages.
        """
        for i in range(config.count):
            await cue.submit("extract", params={"item_id": f"item_{i:04d}"})
            state.submitted += 1
            state.queued += 1
            state.add_event("queued", f"extract_{i}", "extract", f"item_{i:04d}")
            
            if config.submit_rate:
                await asyncio.sleep(1.0 / config.submit_rate)

