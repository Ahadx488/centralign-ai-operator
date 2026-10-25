from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ApprovalStatus(str, Enum):
    PENDING = "PENDING"
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"
    EDITED_APPROVED = "EDITED_APPROVED"
    ESCALATED = "ESCALATED"
    EXPIRED = "EXPIRED"

    @property
    def is_decided(self) -> bool:
        return self != ApprovalStatus.PENDING


class ApprovalRequest(BaseModel):
    approval_id: str = Field(default_factory=lambda: f"appr_{uuid4().hex[:10]}")
    run_id: str
    action: str
    args: dict[str, Any] = Field(default_factory=dict)
    edited_args: dict[str, Any] | None = None
    reason: str = ""
    risk_tier: str = "IRREVERSIBLE_WRITE"
    status: ApprovalStatus = ApprovalStatus.PENDING
    timeout_seconds: float = 300.0
    requested_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    decided_at: str | None = None
    decided_by: str | None = None
    decision_notes: str | None = None


class ApprovalInbox:
    """
    SQLite-backed Approvals Inbox for Human-in-the-Loop policy gating.
    Supports Approve, Reject, Edit-and-Approve, and timeout escalation.
    """

    def __init__(self, db_path: Path | str = "artifacts/runs.db") -> None:
        self.db_path = str(db_path)
        if self.db_path != ":memory:":
            Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        return conn

    def _init_db(self) -> None:
        with self._get_connection() as conn:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS approval_requests (
                    approval_id TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    action TEXT NOT NULL,
                    args_json TEXT,
                    edited_args_json TEXT,
                    reason TEXT,
                    risk_tier TEXT,
                    status TEXT NOT NULL,
                    timeout_seconds REAL DEFAULT 300.0,
                    requested_at TEXT NOT NULL,
                    decided_at TEXT,
                    decided_by TEXT,
                    decision_notes TEXT
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_approvals_status
                ON approval_requests (status);
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_approvals_run_id
                ON approval_requests (run_id);
            """)

    def _row_to_request(self, row: sqlite3.Row) -> ApprovalRequest:
        args = json.loads(row["args_json"]) if row["args_json"] else {}
        edited = json.loads(row["edited_args_json"]) if row["edited_args_json"] else None
        return ApprovalRequest(
            approval_id=row["approval_id"],
            run_id=row["run_id"],
            action=row["action"],
            args=args,
            edited_args=edited,
            reason=row["reason"] or "",
            risk_tier=row["risk_tier"] or "IRREVERSIBLE_WRITE",
            status=ApprovalStatus(row["status"]),
            timeout_seconds=row["timeout_seconds"],
            requested_at=row["requested_at"],
            decided_at=row["decided_at"],
            decided_by=row["decided_by"],
            decision_notes=row["decision_notes"],
        )

    def create_request(
        self,
        run_id: str,
        action: str,
        args: dict[str, Any],
        reason: str,
        risk_tier: str = "IRREVERSIBLE_WRITE",
        timeout_seconds: float = 300.0,
    ) -> ApprovalRequest:
        req = ApprovalRequest(
            run_id=run_id,
            action=action,
            args=args,
            reason=reason,
            risk_tier=risk_tier,
            timeout_seconds=timeout_seconds,
            status=ApprovalStatus.PENDING,
        )
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO approval_requests (
                    approval_id, run_id, action, args_json, edited_args_json,
                    reason, risk_tier, status, timeout_seconds, requested_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    req.approval_id,
                    req.run_id,
                    req.action,
                    json.dumps(req.args, default=str),
                    None,
                    req.reason,
                    req.risk_tier,
                    req.status.value,
                    req.timeout_seconds,
                    req.requested_at,
                ),
            )
        logger.info(f"[ApprovalInbox] Created pending approval request {req.approval_id} for run {run_id}")
        return req

    def get_request(self, approval_id: str) -> ApprovalRequest | None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE approval_id = ?", (approval_id,)
            ).fetchone()
            return self._row_to_request(row) if row else None

    def get_by_run_id(self, run_id: str) -> ApprovalRequest | None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM approval_requests WHERE run_id = ? ORDER BY requested_at DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            return self._row_to_request(row) if row else None

    def list_requests(self, status: ApprovalStatus | None = None, limit: int = 50) -> list[ApprovalRequest]:
        with self._get_connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM approval_requests WHERE status = ? ORDER BY requested_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM approval_requests ORDER BY requested_at DESC LIMIT ?",
                    (limit,),
                ).fetchall()
            return [self._row_to_request(r) for r in rows]

    def get_pending(self) -> list[ApprovalRequest]:
        return self.list_requests(status=ApprovalStatus.PENDING)

    def decide(
        self,
        approval_id: str,
        decision: str,
        edited_args: dict[str, Any] | None = None,
        decided_by: str = "operator",
        notes: str | None = None,
    ) -> ApprovalRequest:
        """
        Record a decision: 'APPROVE', 'REJECT', or 'EDIT_APPROVE'.
        """
        now = datetime.now(timezone.utc).isoformat()
        decision_upper = decision.upper()

        if decision_upper == "APPROVE":
            status = ApprovalStatus.APPROVED
        elif decision_upper == "REJECT":
            status = ApprovalStatus.REJECTED
        elif decision_upper in {"EDIT", "EDIT_APPROVE", "EDITED_APPROVED"}:
            status = ApprovalStatus.EDITED_APPROVED
        else:
            raise ValueError(f"Unknown decision type: {decision}")

        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE approval_requests
                SET status = ?, edited_args_json = ?, decided_at = ?, decided_by = ?, decision_notes = ?
                WHERE approval_id = ?
                """,
                (
                    status.value,
                    json.dumps(edited_args, default=str) if edited_args is not None else None,
                    now,
                    decided_by,
                    notes,
                    approval_id,
                ),
            )
        req = self.get_request(approval_id)
        if not req:
            raise ValueError(f"Approval request {approval_id} not found")
        logger.info(f"[ApprovalInbox] Request {approval_id} decided: {status.value} by {decided_by}")
        return req

    def check_timeouts(self) -> list[ApprovalRequest]:
        """
        Scan all pending approval requests. If elapsed time > timeout_seconds,
        mark as ESCALATED.
        """
        now_dt = datetime.now(timezone.utc)
        escalated: list[ApprovalRequest] = []

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM approval_requests WHERE status = ?",
                (ApprovalStatus.PENDING.value,),
            ).fetchall()

            for r in rows:
                req_dt = datetime.fromisoformat(r["requested_at"])
                elapsed = (now_dt - req_dt).total_seconds()
                limit = r["timeout_seconds"]

                if elapsed > limit:
                    now_str = now_dt.isoformat()
                    conn.execute(
                        """
                        UPDATE approval_requests
                        SET status = ?, decided_at = ?, decision_notes = ?
                        WHERE approval_id = ?
                        """,
                        (
                            ApprovalStatus.ESCALATED.value,
                            now_str,
                            f"Automated escalation: timed out after {elapsed:.1f}s (threshold: {limit}s)",
                            r["approval_id"],
                        ),
                    )
                    escalated.append(self.get_request(r["approval_id"]))  # type: ignore

        return escalated
