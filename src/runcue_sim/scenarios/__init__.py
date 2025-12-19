"""Built-in scenarios for runcue-sim.

Scenarios define workload patterns - services, tasks, dependencies.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import runcue
    from runcue_sim.display import SimulationState
    from runcue_sim.runner import SimConfig


@dataclass
class ScenarioInfo:
    """Metadata about a scenario."""
    name: str
    description: str


class Scenario(ABC):
    """Base class for simulation scenarios.
    
    A scenario defines:
    - Services (rate limits, concurrency)
    - Tasks (handlers, dependencies)
    - Initial workload (what to submit)
    """
    
    @property
    @abstractmethod
    def info(self) -> ScenarioInfo:
        """Return scenario metadata."""
        ...
    
    @abstractmethod
    def setup(self, cue: "runcue.Cue", config: "SimConfig", state: "SimulationState") -> None:
        """Configure services, tasks, and callbacks on the Cue instance.
        
        Args:
            cue: The Cue instance to configure
            config: Simulation configuration (latency, error_rate, etc.)
            state: State object to update for display
        """
        ...
    
    @abstractmethod
    async def submit_workload(self, cue: "runcue.Cue", config: "SimConfig", state: "SimulationState") -> None:
        """Submit the initial workload.
        
        Args:
            cue: The Cue instance to submit to
            config: Simulation configuration
            state: State object to update
        """
        ...


# Import built-in scenarios
from runcue_sim.scenarios.single_queue import SingleQueueScenario
from runcue_sim.scenarios.fanout import FanoutScenario
from runcue_sim.scenarios.pipeline import PipelineScenario
from runcue_sim.scenarios.dynamic import DynamicScenario

# Registry of built-in scenarios
SCENARIOS: dict[str, type[Scenario]] = {
    "single_queue": SingleQueueScenario,
    "fanout": FanoutScenario,
    "pipeline": PipelineScenario,
    "dynamic": DynamicScenario,
}


def get_scenario(name: str) -> Scenario:
    """Get a scenario instance by name."""
    if name not in SCENARIOS:
        available = ", ".join(SCENARIOS.keys())
        raise ValueError(f"Unknown scenario: {name}. Available: {available}")
    return SCENARIOS[name]()


def list_scenarios() -> list[ScenarioInfo]:
    """List all available scenarios."""
    return [cls().info for cls in SCENARIOS.values()]

