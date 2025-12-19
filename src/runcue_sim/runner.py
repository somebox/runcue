"""Simulation runner for runcue-sim.

This module handles the actual simulation logic, decoupled from display.
It updates a SimulationState object that can be rendered by any display.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

import runcue

if TYPE_CHECKING:
    from runcue_sim.display import SimulationState
    from runcue_sim.scenarios import Scenario


@dataclass
class SimConfig:
    """Configuration for a simulation run."""

    count: int = 100
    latency_ms: int = 100
    latency_jitter: float = 0.2  # ±20% variance
    outlier_chance: float = 0.0  # Probability of outlier (0.0-1.0)
    outlier_multiplier: float = 5.0  # Outliers take this much longer
    error_rate: float = 0.0
    duration: float | None = None
    max_concurrent: int = 5
    rate_limit: tuple[int, int] | None = None  # (count, seconds)
    submit_rate: float | None = None  # work/second, None = batch
    scenario: str = "single_queue"  # Scenario name
    stall_timeout: float | None = None  # Auto-stop if stalled for N seconds


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
        state: SimulationState,
        on_event: Callable[[str, str, str | None, str], None] | None = None,
    ):
        self.config = config
        self.state = state
        self.on_event = on_event or state.add_event
        
        self._cue: runcue.Cue | None = None
        self._scenario: Scenario | None = None
        self._running = False
    
    async def run(self) -> None:
        """Run the simulation to completion."""
        from runcue_sim.scenarios import get_scenario
        
        self._running = True
        self.state.start_time = time.time()
        self.state.target_count = self.config.count
        self.state.latency_ms = self.config.latency_ms
        self.state.latency_jitter = self.config.latency_jitter
        self.state.outlier_chance = self.config.outlier_chance
        self.state.error_rate = self.config.error_rate
        self.state.scenario_name = self.config.scenario
        
        # Create orchestrator (stateless, no db)
        self._cue = runcue.Cue()
        
        # Get and setup scenario
        self._scenario = get_scenario(self.config.scenario)
        self._scenario.setup(self._cue, self.config, self.state)
        
        # Start orchestrator
        self._cue.start()
        
        # Submit workload via scenario
        await self._scenario.submit_workload(self._cue, self.config, self.state)
        
        # Monitor until complete
        await self._monitor()
        
        # Cleanup
        await self._cue.stop()
        self._running = False
    
    async def _monitor(self) -> None:
        """Monitor until all work completes or duration exceeded."""
        while self._running:
            # Update elapsed time
            self.state.elapsed = self._elapsed
            
            # Check completion (all submitted work is done)
            total_done = self.state.completed + self.state.failed
            if total_done >= self.state.submitted and self.state.running == 0:
                break
            
            # Check duration limit
            if self.config.duration and self._elapsed >= self.config.duration:
                break
            
            await asyncio.sleep(0.05)
    
    @property
    def _elapsed(self) -> float:
        """Elapsed time since start."""
        return time.time() - self.state.start_time
    
    def stop(self) -> None:
        """Request simulation stop."""
        self._running = False
    
    async def cleanup(self) -> None:
        """Clean up resources. Call after interrupt or completion."""
        if self._cue:
            try:
                await self._cue.stop()
            except Exception:
                pass  # Ignore errors during cleanup
            self._cue = None
        self._running = False
    
    def debug_blocked(self) -> list[dict]:
        """Get diagnostic info about blocked work.
        
        Returns list of dicts with work, reason, and details.
        """
        if not self._cue:
            return []
        return self._cue.debug_blocked()
    
    @property
    def cue(self) -> runcue.Cue | None:
        """Access to underlying Cue instance for debugging."""
        return self._cue
