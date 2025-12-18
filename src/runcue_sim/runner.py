"""Simulation runner for runcue-sim.

This module handles the actual simulation logic, decoupled from display.
It updates a SimulationState object that can be rendered by any display.
"""

from __future__ import annotations

import asyncio
import random
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

import runcue
from runcue import WorkState

if TYPE_CHECKING:
    from runcue_sim.display import SimulationState


@dataclass
class SimConfig:
    """Configuration for a simulation run."""

    count: int = 100
    latency_ms: int = 100
    error_rate: float = 0.0
    duration: float | None = None
    db_path: str = ":memory:"
    max_concurrent: int = 5
    rate_limit: tuple[int, int] | None = None
    submit_rate: float | None = None  # work/second, None = batch


class SimulationRunner:
    """Runs simulations and updates state for display.
    
    This class is decoupled from display - it just updates state.
    The display polls state to render.
    
    Usage:
        config = SimConfig(count=100, latency_ms=50)
        state = SimulationState()
        runner = SimulationRunner(config, state)
        
        # In your event loop:
        await runner.run()
    """
    
    def __init__(
        self,
        config: SimConfig,
        state: "SimulationState",
        on_event: Callable[[str, str, str | None, str], None] | None = None,
    ):
        self.config = config
        self.state = state
        self.on_event = on_event or state.add_event
        
        self._cue: runcue.Cue | None = None
        self._running = False
        self._work_started: dict[str, float] = {}  # work_id -> start_time
    
    async def run(self) -> None:
        """Run the simulation to completion."""
        self._running = True
        self.state.start_time = time.time()
        self.state.target_count = self.config.count
        self.state.latency_ms = self.config.latency_ms
        self.state.error_rate = self.config.error_rate
        
        # Create orchestrator
        self._cue = runcue.Cue(self.config.db_path)
        
        # Register service
        self._cue.service(
            "mock_api",
            max_concurrent=self.config.max_concurrent,
            rate_limit=self.config.rate_limit,
        )
        
        # Update service display
        from runcue_sim.display import ServiceStatus
        self.state.services["mock_api"] = ServiceStatus(
            name="mock_api",
            max_concurrent=self.config.max_concurrent,
            rate_limit=self.config.rate_limit[0] if self.config.rate_limit else None,
            rate_window=self.config.rate_limit[1] if self.config.rate_limit else None,
        )
        
        # Register mock task
        @self._cue.task("mock_work", services=["mock_api"])
        async def mock_handler(work):
            work_id = work.id
            self._work_started[work_id] = time.time()
            self.on_event("started", work_id, "mock_work", work.target or "")
            
            try:
                # Simulate latency
                actual_latency = self.config.latency_ms / 1000.0
                if actual_latency > 0:
                    actual_latency *= random.uniform(0.8, 1.2)
                    await asyncio.sleep(actual_latency)
                
                # Simulate errors
                if random.random() < self.config.error_rate:
                    raise RuntimeError("Simulated error")
                
                duration_ms = int((time.time() - self._work_started.get(work_id, time.time())) * 1000)
                self.on_event("completed", work_id, "mock_work", f"{duration_ms}ms")
                
                return {"mock": True, "latency_ms": int(actual_latency * 1000)}
                
            except Exception as e:
                self.on_event("failed", work_id, "mock_work", str(e))
                raise
            finally:
                self._work_started.pop(work_id, None)
        
        # Start orchestrator
        self._cue.start()
        
        # Submit work
        await self._submit_work()
        
        # Monitor until complete
        await self._monitor()
        
        # Cleanup
        await self._cue.stop()
        await self._cue.close()
        self._running = False
    
    async def _submit_work(self) -> None:
        """Submit work units according to config."""
        for i in range(self.config.count):
            if not self._running:
                break
            
            await self._cue.submit("mock_work", target=f"item_{i:04d}")
            self.state.submitted += 1
            self.on_event("queued", f"work_{i}", "mock_work", f"item_{i:04d}")
            
            # Rate-limited submission
            if self.config.submit_rate:
                await asyncio.sleep(1.0 / self.config.submit_rate)
            
            # Check duration limit during submission
            if self.config.duration and self._elapsed >= self.config.duration:
                break
    
    async def _monitor(self) -> None:
        """Monitor until all work completes or duration exceeded."""
        while self._running:
            # Update state from queue
            await self._update_state()
            
            # Check completion
            if self.state.queued == 0 and self.state.running == 0:
                break
            
            # Check duration limit
            if self.config.duration and self._elapsed >= self.config.duration:
                break
            
            await asyncio.sleep(0.05)
    
    async def _update_state(self) -> None:
        """Update simulation state from the queue."""
        if not self._cue:
            return
        
        self.state.elapsed = self._elapsed
        
        # Query current counts
        queued = await self._cue.list(state=WorkState.QUEUED)
        running = await self._cue.list(state=WorkState.RUNNING)
        completed = await self._cue.list(state=WorkState.COMPLETED)
        failed = await self._cue.list(state=WorkState.FAILED)
        
        self.state.queued = len(queued)
        self.state.running = len(running)
        self.state.completed = len(completed)
        self.state.failed = len(failed)
        
        # Update service concurrent count from actual running work
        svc = self.state.services.get("mock_api")
        if svc:
            svc.current_concurrent = len(running)
    
    @property
    def _elapsed(self) -> float:
        """Elapsed time since start."""
        return time.time() - self.state.start_time
    
    def stop(self) -> None:
        """Request simulation stop."""
        self._running = False

