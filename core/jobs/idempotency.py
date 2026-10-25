from __future__ import annotations

import hashlib
import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.tools.registry import ToolResult

logger = logging.getLogger(__name__)


class IdempotencyStore:
    """
    Durable idempotency key store for write tool executions.
    Guarantees that re-running or resuming a crashed run never double-submits
    sensitive actions like ERP bill postings, ticket creations, or emails.
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
                CREATE TABLE IF NOT EXISTS idempotency_keys (
                    idempotency_key TEXT PRIMARY KEY,
                    run_id TEXT NOT NULL,
                    tool_name TEXT NOT NULL,
                    args_hash TEXT NOT NULL,
                    args_json TEXT,
                    result_ok INTEGER NOT NULL,
                    result_data_json TEXT,
                    result_error TEXT,
                    evidence_refs_json TEXT,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_idempotency_run_id
                ON idempotency_keys (run_id);
            """)

    @staticmethod
    def compute_key(run_id: str, tool_name: str, args: dict[str, Any], context_hint: str = "") -> str:
        """Deterministically compute an idempotency key from run_id, tool, context hint, and sorted args."""
        args_sorted = json.dumps(args, sort_keys=True, default=str)
        raw = f"{run_id}:{tool_name}:{context_hint}:{args_sorted}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def is_executed(self, key: str) -> bool:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT 1 FROM idempotency_keys WHERE idempotency_key = ?", (key,)
            ).fetchone()
            return row is not None

    def get_cached_result(self, key: str) -> ToolResult | None:
        with self._get_connection() as conn:
            row = conn.execute(
                "SELECT * FROM idempotency_keys WHERE idempotency_key = ?", (key,)
            ).fetchone()
            if not row:
                return None

            data = json.loads(row["result_data_json"]) if row["result_data_json"] else None
            ev_refs = json.loads(row["evidence_refs_json"]) if row["evidence_refs_json"] else []
            return ToolResult(
                ok=bool(row["result_ok"]),
                data=data,
                error=row["result_error"],
                evidence_refs=ev_refs,
            )

    def record_execution(
        self,
        key: str,
        run_id: str,
        tool_name: str,
        args: dict[str, Any],
        result: ToolResult,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        args_sorted = json.dumps(args, sort_keys=True, default=str)
        args_hash = hashlib.sha256(args_sorted.encode("utf-8")).hexdigest()

        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO idempotency_keys (
                    idempotency_key, run_id, tool_name, args_hash, args_json,
                    result_ok, result_data_json, result_error, evidence_refs_json, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    key,
                    run_id,
                    tool_name,
                    args_hash,
                    args_sorted,
                    1 if result.ok else 0,
                    json.dumps(result.data, default=str) if result.data is not None else None,
                    result.error,
                    json.dumps(result.evidence_refs),
                    now,
                ),
            )
        logger.info(f"[Idempotency] Recorded write execution for {tool_name} (key: {key[:8]}...)")
