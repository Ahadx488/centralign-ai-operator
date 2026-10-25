from __future__ import annotations

import datetime
import logging
import sqlite3
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)

DEFAULT_DB_PATH = Path("./artifacts/memory.db")


class ProposedFact(BaseModel):
    """A fact learned during a run with provenance, awaiting promotion to company memory."""

    id: int | None = None
    run_id: str
    step_index: int
    category: str
    key: str
    value: str
    confidence: float = 1.0
    status: str = "PROPOSED"  # "PROPOSED", "PROMOTED", "REJECTED"
    created_at: str = Field(default_factory=lambda: datetime.datetime.now(datetime.timezone.utc).isoformat())
    promoted_at: str | None = None


class ProposedMemoryStore:
    """
    SQLite-backed store for facts discovered during agent execution.
    Preserves run/step provenance and controls promotion into active company memory.
    """

    def __init__(self, db_path: Path | str = DEFAULT_DB_PATH) -> None:
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _get_conn(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._get_conn() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS proposed_memory (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    run_id TEXT NOT NULL,
                    step_index INTEGER NOT NULL,
                    category TEXT NOT NULL,
                    key TEXT NOT NULL,
                    value TEXT NOT NULL,
                    confidence REAL DEFAULT 1.0,
                    status TEXT DEFAULT 'PROPOSED',
                    created_at TEXT NOT NULL,
                    promoted_at TEXT
                );
                """
            )
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proposed_run ON proposed_memory(run_id);")
            conn.execute("CREATE INDEX IF NOT EXISTS idx_proposed_status ON proposed_memory(status);")

    def record_fact(
        self,
        run_id: str,
        step_index: int,
        category: str,
        key: str,
        value: str,
        confidence: float = 1.0,
    ) -> ProposedFact:
        """Record a newly discovered fact with execution provenance."""
        created_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            cur = conn.execute(
                """
                INSERT INTO proposed_memory (run_id, step_index, category, key, value, confidence, status, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 'PROPOSED', ?)
                """,
                (run_id, step_index, category, key, value, confidence, created_at),
            )
            fact_id = cur.lastrowid

        fact = ProposedFact(
            id=fact_id,
            run_id=run_id,
            step_index=step_index,
            category=category,
            key=key,
            value=value,
            confidence=confidence,
            status="PROPOSED",
            created_at=created_at,
        )
        logger.info(f"[Proposed Memory] Fact recorded from run {run_id} (step {step_index}): {key} -> {value}")
        return fact

    def get_fact(self, fact_id: int) -> ProposedFact | None:
        """Retrieve a specific fact by ID."""
        with self._get_conn() as conn:
            row = conn.execute("SELECT * FROM proposed_memory WHERE id = ?", (fact_id,)).fetchone()
            if not row:
                return None
            return ProposedFact(
                id=row["id"],
                run_id=row["run_id"],
                step_index=row["step_index"],
                category=row["category"],
                key=row["key"],
                value=row["value"],
                confidence=row["confidence"],
                status=row["status"],
                created_at=row["created_at"],
                promoted_at=row["promoted_at"],
            )

    def get_facts(
        self,
        run_id: str | None = None,
        status: str | None = None,
    ) -> list[ProposedFact]:
        """Query proposed facts filtered by run or status."""
        sql = "SELECT * FROM proposed_memory WHERE 1=1"
        params: list[Any] = []
        if run_id:
            sql += " AND run_id = ?"
            params.append(run_id)
        if status:
            sql += " AND status = ?"
            params.append(status)
        sql += " ORDER BY id DESC"

        with self._get_conn() as conn:
            cur = conn.execute(sql, params)
            return [
                ProposedFact(
                    id=row["id"],
                    run_id=row["run_id"],
                    step_index=row["step_index"],
                    category=row["category"],
                    key=row["key"],
                    value=row["value"],
                    confidence=row["confidence"],
                    status=row["status"],
                    created_at=row["created_at"],
                    promoted_at=row["promoted_at"],
                )
                for row in cur.fetchall()
            ]

    def promote_fact(self, fact_id: int) -> bool:
        """Promote a proposed fact into active verified company memory."""
        promoted_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
        with self._get_conn() as conn:
            res = conn.execute(
                "UPDATE proposed_memory SET status = 'PROMOTED', promoted_at = ? WHERE id = ? AND status = 'PROPOSED'",
                (promoted_at, fact_id),
            )
            promoted = res.rowcount > 0

        if promoted:
            logger.info(f"[Proposed Memory] Fact #{fact_id} successfully promoted to active company memory.")
        return promoted

    def reject_fact(self, fact_id: int) -> bool:
        """Reject a proposed fact."""
        with self._get_conn() as conn:
            res = conn.execute(
                "UPDATE proposed_memory SET status = 'REJECTED' WHERE id = ? AND status = 'PROPOSED'",
                (fact_id,),
            )
            return res.rowcount > 0

    def get_promoted_facts(self) -> list[dict[str, Any]]:
        """Return all active promoted facts formatted for company memory indexing."""
        with self._get_conn() as conn:
            cur = conn.execute("SELECT * FROM proposed_memory WHERE status = 'PROMOTED'")
            return [dict(row) for row in cur.fetchall()]
