from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field


class JobStatus(str, Enum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

    @property
    def is_terminal(self) -> bool:
        return self in {JobStatus.COMPLETED, JobStatus.FAILED}


class Job(BaseModel):
    job_id: str = Field(default_factory=lambda: f"job_{uuid4().hex[:10]}")
    run_id: str | None = None
    task: str
    status: JobStatus = JobStatus.QUEUED
    cron_expression: str | None = None
    next_run_at: str | None = None
    retry_count: int = 0
    max_retries: int = 3
    worker_id: str | None = None
    heartbeat_at: str | None = None
    result_data: dict[str, Any] | None = None
    error: str | None = None
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    updated_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
