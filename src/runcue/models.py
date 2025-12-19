"""Core data models for runcue."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkState(str, Enum):
    """Possible states for a work unit."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass
class WorkUnit:
    """A request to perform work."""

    id: str
    task: str  # Task type name
    params: dict[str, Any] = field(default_factory=dict)
    state: WorkState = WorkState.PENDING
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    attempt: int = 1


@dataclass
class TaskType:
    """Defines how to dispatch a category of work."""

    name: str
    service: str | None = None  # Service this task uses (single)
    handler: Any = None
    retry: int = 1  # Max attempts


@dataclass
class PriorityContext:
    """Context passed to priority callback."""

    work: WorkUnit
    wait_time: float  # Seconds since created_at
    queue_depth: int  # Total pending work count
