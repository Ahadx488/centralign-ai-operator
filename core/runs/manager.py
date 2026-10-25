from __future__ import annotations

import json
import logging
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from uuid import uuid4

from core.runs.models import (
    ALLOWED_TRANSITIONS,
    ArtifactRecord,
    RunRecord,
    RunStatus,
    StepRecord,
    TransitionRecord,
)

logger = logging.getLogger(__name__)


class RunManager:
    """
    Durable persistence layer for CentrAlign agent runs, state transitions,
    working memory, audit trail steps, and artifacts using SQLite.
    """

    def __init__(
        self,
        db_path: Path | str = "data/centralign.db",
        artifacts_dir: Path | str = "data/artifacts",
    ) -> None:
        self.db_path = str(db_path)
        self.artifacts_dir = Path(artifacts_dir)
        self.artifacts_dir.mkdir(parents=True, exist_ok=True)

        if self.db_path != ":memory:":
            os.makedirs(os.path.dirname(os.path.abspath(self.db_path)), exist_ok=True)

        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.executescript("""
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    goal_json TEXT,
                    plan_json TEXT,
                    working_memory_json TEXT,
                    step_count INTEGER DEFAULT 0,
                    tokens_used INTEGER DEFAULT 0,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS run_transitions (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    from_status TEXT NOT NULL,
                    to_status TEXT NOT NULL,
                    reason TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id)
                );

                CREATE TABLE IF NOT EXISTS run_steps (
                    step_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    intent_id TEXT,
                    thought TEXT,
                    tool_name TEXT,
                    tool_args_json TEXT,
                    observation_summary TEXT,
                    observation_data_json TEXT,
                    ok INTEGER NOT NULL,
                    error TEXT,
                    evidence_refs_json TEXT,
                    duration_ms REAL DEFAULT 0.0,
                    tokens INTEGER DEFAULT 0,
                    policy_decision TEXT,
                    policy_reason TEXT,
                    timestamp TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id)
                );

                CREATE TABLE IF NOT EXISTS artifacts (
                    artifact_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    name TEXT NOT NULL,
                    mime_type TEXT NOT NULL,
                    size_bytes INTEGER NOT NULL,
                    file_path TEXT NOT NULL,
                    summary TEXT,
                    created_at TEXT NOT NULL,
                    FOREIGN KEY (run_id) REFERENCES runs (run_id)
                );

                CREATE INDEX IF NOT EXISTS idx_transitions_run_id ON run_transitions (run_id);
                CREATE INDEX IF NOT EXISTS idx_steps_run_id ON run_steps (run_id);
                CREATE INDEX IF NOT EXISTS idx_artifacts_run_id ON artifacts (run_id);
            """)
            try:
                conn.execute("ALTER TABLE run_steps ADD COLUMN policy_decision TEXT")
            except Exception:
                pass
            try:
                conn.execute("ALTER TABLE run_steps ADD COLUMN policy_reason TEXT")
            except Exception:
                pass

    def create_run(self, task: str, run_id: str | None = None) -> RunRecord:
        record = RunRecord(
            run_id=run_id or f"run_{uuid4().hex[:12]}",
            task=task,
            status=RunStatus.CREATED,
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO runs (
                    run_id, task, status, goal_json, plan_json,
                    working_memory_json, step_count, tokens_used, error,
                    created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    record.run_id,
                    record.task,
                    record.status.value,
                    json.dumps(record.goal) if record.goal else None,
                    json.dumps(record.plan) if record.plan else None,
                    json.dumps(record.working_memory),
                    record.step_count,
                    record.tokens_used,
                    record.error,
                    now,
                    now,
                ),
            )
            # Record initial transition from NONE -> CREATED
            conn.execute(
                """
                INSERT INTO run_transitions (run_id, from_status, to_status, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (record.run_id, "NONE", RunStatus.CREATED.value, "Run initiated", now),
            )
        return record

    def get_run(self, run_id: str) -> RunRecord | None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM runs WHERE run_id = ?", (run_id,)
            ).fetchone()
            if not row:
                return None
            return self._row_to_run(row)

    def list_runs(self, limit: int = 50) -> list[RunRecord]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM runs ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_run(r) for r in rows]

    def transition(
        self,
        run_id: str,
        to_status: RunStatus,
        reason: str = "",
        force: bool = False,
    ) -> RunRecord:
        """
        Transition run to a new state with strict state machine validation.
        """
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        current = run.status
        if current == to_status:
            return run

        allowed = ALLOWED_TRANSITIONS.get(current, set())
        if not force and to_status not in allowed:
            raise ValueError(
                f"Invalid transition from {current.value} to {to_status.value} for run {run_id}. "
                f"Allowed destinations: {[s.value for s in allowed]}"
            )

        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (to_status.value, now, run_id),
            )
            conn.execute(
                """
                INSERT INTO run_transitions (run_id, from_status, to_status, reason, timestamp)
                VALUES (?, ?, ?, ?, ?)
                """,
                (run_id, current.value, to_status.value, reason, now),
            )

        run.status = to_status
        run.updated_at = now
        logger.info(f"Run {run_id} transitioned: {current.value} -> {to_status.value} (Reason: {reason})")
        return run

    def update_run(
        self,
        run_id: str,
        goal: dict[str, Any] | None = None,
        plan: dict[str, Any] | None = None,
        working_memory: dict[str, Any] | None = None,
        step_count: int | None = None,
        tokens_used: int | None = None,
        error: str | None = None,
    ) -> RunRecord:
        run = self.get_run(run_id)
        if not run:
            raise ValueError(f"Run {run_id} not found")

        updates: list[str] = []
        params: list[Any] = []

        if goal is not None:
            updates.append("goal_json = ?")
            params.append(json.dumps(goal))
            run.goal = goal

        if plan is not None:
            updates.append("plan_json = ?")
            params.append(json.dumps(plan))
            run.plan = plan

        if working_memory is not None:
            updates.append("working_memory_json = ?")
            params.append(json.dumps(working_memory))
            run.working_memory = working_memory

        if step_count is not None:
            updates.append("step_count = ?")
            params.append(step_count)
            run.step_count = step_count

        if tokens_used is not None:
            updates.append("tokens_used = ?")
            params.append(tokens_used)
            run.tokens_used = tokens_used

        if error is not None:
            updates.append("error = ?")
            params.append(error)
            run.error = error

        now = datetime.now(timezone.utc).isoformat()
        updates.append("updated_at = ?")
        params.append(now)
        run.updated_at = now

        params.append(run_id)
        sql = f"UPDATE runs SET {', '.join(updates)} WHERE run_id = ?"

        with self._get_connection() as conn:
            conn.execute(sql, tuple(params))

        return run

    def record_step(self, run_id: str, step: StepRecord) -> None:
        """
        Append step record to immutable audit log.
        """
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO run_steps (
                    step_id, run_id, step_index, intent_id, thought, tool_name,
                    tool_args_json, observation_summary, observation_data_json,
                    ok, error, evidence_refs_json, duration_ms, tokens,
                    policy_decision, policy_reason, timestamp
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    step.step_id,
                    run_id,
                    step.step_index,
                    step.intent_id,
                    step.thought,
                    step.tool_name,
                    json.dumps(step.tool_args),
                    step.observation_summary,
                    json.dumps(step.observation_data) if step.observation_data is not None else None,
                    1 if step.ok else 0,
                    step.error,
                    json.dumps(step.evidence_refs),
                    step.duration_ms,
                    step.tokens,
                    step.policy_decision,
                    step.policy_reason,
                    step.timestamp,
                ),
            )
            # Also increment run step_count and tokens_used
            conn.execute(
                """
                UPDATE runs
                SET step_count = step_count + 1,
                    tokens_used = tokens_used + ?,
                    updated_at = ?
                WHERE run_id = ?
                """,
                (step.tokens, step.timestamp, run_id),
            )

    def get_steps(self, run_id: str) -> list[StepRecord]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_steps WHERE run_id = ? ORDER BY step_index ASC",
                (run_id,),
            ).fetchall()
            return [
                StepRecord(
                    step_id=r["step_id"],
                    run_id=r["run_id"],
                    step_index=r["step_index"],
                    intent_id=r["intent_id"],
                    thought=r["thought"] or "",
                    tool_name=r["tool_name"],
                    tool_args=json.loads(r["tool_args_json"]) if r["tool_args_json"] else {},
                    observation_summary=r["observation_summary"] or "",
                    observation_data=json.loads(r["observation_data_json"]) if r["observation_data_json"] else None,
                    ok=bool(r["ok"]),
                    error=r["error"],
                    evidence_refs=json.loads(r["evidence_refs_json"]) if r["evidence_refs_json"] else [],
                    duration_ms=r["duration_ms"] or 0.0,
                    tokens=r["tokens"] or 0,
                    policy_decision=r["policy_decision"] if "policy_decision" in r.keys() else None,
                    policy_reason=r["policy_reason"] if "policy_reason" in r.keys() else None,
                    timestamp=r["timestamp"],
                )
                for r in rows
            ]

    def get_transitions(self, run_id: str) -> list[TransitionRecord]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM run_transitions WHERE run_id = ? ORDER BY id ASC",
                (run_id,),
            ).fetchall()
            return [
                TransitionRecord(
                    run_id=r["run_id"],
                    from_status=RunStatus(r["from_status"]) if r["from_status"] != "NONE" else RunStatus.CREATED,
                    to_status=RunStatus(r["to_status"]),
                    reason=r["reason"] or "",
                    timestamp=r["timestamp"],
                )
                for r in rows
            ]

    def save_artifact(
        self,
        run_id: str,
        name: str,
        content: str | bytes,
        summary: str = "",
        mime_type: str = "text/plain",
    ) -> ArtifactRecord:
        """Save large output to artifact directory and record metadata in DB."""
        run_art_dir = self.artifacts_dir / run_id
        run_art_dir.mkdir(parents=True, exist_ok=True)

        artifact_id = f"art_{uuid4().hex[:8]}"
        ext = ".bin"
        if "json" in mime_type:
            ext = ".json"
        elif "html" in mime_type:
            ext = ".html"
        elif "text" in mime_type or "plain" in mime_type:
            ext = ".txt"
        elif "pdf" in mime_type:
            ext = ".pdf"
        elif "png" in mime_type:
            ext = ".png"

        clean_name = "".join(c for c in name if c.isalnum() or c in ("-", "_", ".")).strip()
        filename = f"{artifact_id}_{clean_name}{ext}" if not clean_name.endswith(ext) else f"{artifact_id}_{clean_name}"
        file_path = run_art_dir / filename

        if isinstance(content, str):
            file_path.write_text(content, encoding="utf-8")
            size_bytes = len(content.encode("utf-8"))
        else:
            file_path.write_bytes(content)
            size_bytes = len(content)

        now = datetime.now(timezone.utc).isoformat()
        artifact = ArtifactRecord(
            artifact_id=artifact_id,
            run_id=run_id,
            name=name,
            mime_type=mime_type,
            size_bytes=size_bytes,
            file_path=str(file_path),
            summary=summary,
            created_at=now,
        )

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO artifacts (
                    artifact_id, run_id, name, mime_type, size_bytes,
                    file_path, summary, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    artifact.artifact_id,
                    artifact.run_id,
                    artifact.name,
                    artifact.mime_type,
                    artifact.size_bytes,
                    artifact.file_path,
                    artifact.summary,
                    artifact.created_at,
                ),
            )

        return artifact

    def get_artifacts(self, run_id: str) -> list[ArtifactRecord]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at ASC",
                (run_id,),
            ).fetchall()
            return [
                ArtifactRecord(
                    artifact_id=r["artifact_id"],
                    run_id=r["run_id"],
                    name=r["name"],
                    mime_type=r["mime_type"],
                    size_bytes=r["size_bytes"],
                    file_path=r["file_path"],
                    summary=r["summary"] or "",
                    created_at=r["created_at"],
                )
                for r in rows
            ]

    @staticmethod
    def _row_to_run(row: sqlite3.Row) -> RunRecord:
        return RunRecord(
            run_id=row["run_id"],
            task=row["task"],
            status=RunStatus(row["status"]),
            goal=json.loads(row["goal_json"]) if row["goal_json"] else None,
            plan=json.loads(row["plan_json"]) if row["plan_json"] else None,
            working_memory=json.loads(row["working_memory_json"]) if row["working_memory_json"] else {},
            step_count=row["step_count"],
            tokens_used=row["tokens_used"],
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )
