"""Core data models for runcue."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from enum import Enum
from typing import Any


class WorkState(str, Enum):
    """Possible states for a work unit."""

    QUEUED = "queued"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"


class CircuitState(str, Enum):
    """Circuit breaker states."""

    CLOSED = "closed"
    OPEN = "open"
    HALF_OPEN = "half_open"


@dataclass
class Service:
    """A rate-limited or capacity-limited external resource."""

    name: str
    max_concurrent: int | None = None
    rate_limit: int | None = None  # requests per window
    rate_window: int | None = None  # window in seconds
    circuit_state: CircuitState = CircuitState.CLOSED
    circuit_threshold: int = 5  # failures before opening
    circuit_timeout: int = 300  # seconds before half-open
    circuit_failures: int = 0
    circuit_last_failure: float | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Service:
        """Create Service from database row."""
        return cls(
            name=row["name"],
            max_concurrent=row["max_concurrent"],
            rate_limit=row["rate_limit"],
            rate_window=row["rate_window"],
            circuit_state=CircuitState(row["circuit_state"]),
            circuit_threshold=row["circuit_threshold"],
            circuit_timeout=row["circuit_timeout"],
            circuit_failures=row["circuit_failures"],
            circuit_last_failure=row["circuit_last_failure"],
        )


@dataclass
class TaskType:
    """Defines how to dispatch a category of work."""

    name: str
    services: list[str] = field(default_factory=list)
    executor: str = "async"  # async, process_pool, subprocess
    handler: Any = None  # The callable
    retry_max_attempts: int = 1
    retry_backoff: str = "exponential"
    retry_base_delay: float = 1.0
    retry_max_delay: float = 300.0


@dataclass
class WorkUnit:
    """A request to perform work."""

    id: str
    task_type: str
    target: str | None = None
    params: dict[str, Any] = field(default_factory=dict)
    state: WorkState = WorkState.QUEUED
    created_at: float = 0.0
    started_at: float | None = None
    completed_at: float | None = None
    result: dict[str, Any] | None = None
    error: str | None = None
    attempt: int = 1
    max_attempts: int = 1
    next_retry_at: float | None = None
    depends_on: list[str] = field(default_factory=list)
    idempotency_key: str | None = None
    timeout: float = 300.0  # 5 minutes default
    dependency_timeout: float | None = None
    leased_by: str | None = None
    lease_expires_at: float | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> WorkUnit:
        """Create WorkUnit from database row."""
        return cls(
            id=row["id"],
            task_type=row["task_type"],
            target=row["target"],
            params=json.loads(row["params"]) if row["params"] else {},
            state=WorkState(row["state"]),
            created_at=row["created_at"],
            started_at=row["started_at"],
            completed_at=row["completed_at"],
            result=json.loads(row["result"]) if row["result"] else None,
            error=row["error"],
            attempt=row["attempt"],
            max_attempts=row["max_attempts"],
            next_retry_at=row["next_retry_at"],
            depends_on=json.loads(row["depends_on"]) if row["depends_on"] else [],
            idempotency_key=row["idempotency_key"],
            timeout=row["timeout"],
            dependency_timeout=row["dependency_timeout"],
            leased_by=row["leased_by"],
            lease_expires_at=row["lease_expires_at"],
        )

    def to_row(self) -> dict[str, Any]:
        """Convert to database row dict."""
        return {
            "id": self.id,
            "task_type": self.task_type,
            "target": self.target,
            "params": json.dumps(self.params) if self.params else None,
            "state": self.state.value,
            "created_at": self.created_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "result": json.dumps(self.result) if self.result else None,
            "error": self.error,
            "attempt": self.attempt,
            "max_attempts": self.max_attempts,
            "next_retry_at": self.next_retry_at,
            "depends_on": json.dumps(self.depends_on) if self.depends_on else None,
            "idempotency_key": self.idempotency_key,
            "timeout": self.timeout,
            "dependency_timeout": self.dependency_timeout,
            "leased_by": self.leased_by,
            "lease_expires_at": self.lease_expires_at,
        }

