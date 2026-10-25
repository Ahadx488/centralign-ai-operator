from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class RunStatus(str, Enum):
    CREATED = "CREATED"
    CLARIFYING = "CLARIFYING"
    PLANNING = "PLANNING"
    RUNNING = "RUNNING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    WAITING_USER = "WAITING_USER"
    VERIFYING = "VERIFYING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    ESCALATED = "ESCALATED"

    @property
    def is_terminal(self) -> bool:
        return self in {RunStatus.COMPLETED, RunStatus.FAILED, RunStatus.ESCALATED}

    @property
    def is_paused(self) -> bool:
        return self in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_USER}


# Valid state machine transitions
ALLOWED_TRANSITIONS: dict[RunStatus, set[RunStatus]] = {
    RunStatus.CREATED: {
        RunStatus.CLARIFYING,
        RunStatus.PLANNING,
        RunStatus.FAILED,
    },
    RunStatus.CLARIFYING: {
        RunStatus.PLANNING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED,
        RunStatus.ESCALATED,
    },
    RunStatus.PLANNING: {
        RunStatus.RUNNING,
        RunStatus.WAITING_USER,
        RunStatus.FAILED,
        RunStatus.ESCALATED,
    },
    RunStatus.RUNNING: {
        RunStatus.WAITING_APPROVAL,
        RunStatus.WAITING_USER,
        RunStatus.PLANNING,  # Re-planning
        RunStatus.VERIFYING,
        RunStatus.COMPLETED,
        RunStatus.FAILED,
        RunStatus.ESCALATED,
    },
    RunStatus.WAITING_APPROVAL: {
        RunStatus.RUNNING,  # Approved
        RunStatus.FAILED,   # Rejected
        RunStatus.ESCALATED,# Timeout/escalated
    },
    RunStatus.WAITING_USER: {
        RunStatus.PLANNING, # Resumed with user clarification
        RunStatus.RUNNING,  # Direct resumption
        RunStatus.FAILED,
        RunStatus.ESCALATED,
    },
    RunStatus.VERIFYING: {
        RunStatus.COMPLETED,
        RunStatus.RUNNING,  # Bounded fix attempt on verification failure
        RunStatus.FAILED,
        RunStatus.ESCALATED,
    },
    # Terminal states can be re-opened for recovery/resumption if explicitly requested
    RunStatus.COMPLETED: set(),
    RunStatus.FAILED: {RunStatus.RUNNING, RunStatus.PLANNING},
    RunStatus.ESCALATED: {RunStatus.RUNNING, RunStatus.PLANNING},
}


class TransitionRecord(BaseModel):
    run_id: str
    from_status: RunStatus
    to_status: RunStatus
    reason: str = ""
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class StepRecord(BaseModel):
    step_id: str = Field(default_factory=lambda: f"step_{uuid4().hex[:8]}")
    run_id: str
    step_index: int
    intent_id: str | None = None
    thought: str = ""
    tool_name: str | None = None
    tool_args: dict[str, Any] = Field(default_factory=dict)
    observation_summary: str = ""
    observation_data: Any | None = None
    ok: bool = True
    error: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)
    duration_ms: float = 0.0
    tokens: int = 0
    policy_decision: str | None = None
    policy_reason: str | None = None
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class ArtifactRecord(BaseModel):
    artifact_id: str = Field(default_factory=lambda: f"art_{uuid4().hex[:8]}")
    run_id: str
    name: str
    mime_type: str = "text/plain"
    size_bytes: int = 0
    file_path: str = ""
    summary: str = ""
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class RunRecord(BaseModel):
    run_id: str = Field(default_factory=lambda: f"run_{uuid4().hex[:12]}")
    task: str
    status: RunStatus = RunStatus.CREATED
    goal: dict[str, Any] | None = None
    plan: dict[str, Any] | None = None
    working_memory: dict[str, Any] = Field(default_factory=dict)
    step_count: int = 0
    tokens_used: int = 0
    error: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
