from __future__ import annotations

import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class Lesson(BaseModel):
    lesson_id: str = Field(default_factory=lambda: f"les_{uuid4().hex[:10]}")
    trigger_pattern: str
    category: str
    observation_error: str | None = None
    learned_solution: str
    evidence_run_id: str | None = None
    confidence: float = 0.9
    applied_count: int = 0
    created_at: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )


class LessonStore:
    """
    SQLite-backed repository of operational lessons learned across runs.
    Enables automatic learning from failures, recoveries, and human feedback.
    Future runs retrieve relevant lessons at Plan/Execute time to complete
    fault-prone workflows in strictly fewer steps.
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
                CREATE TABLE IF NOT EXISTS lessons (
                    lesson_id TEXT PRIMARY KEY,
                    trigger_pattern TEXT NOT NULL,
                    category TEXT NOT NULL,
                    observation_error TEXT,
                    learned_solution TEXT NOT NULL,
                    evidence_run_id TEXT,
                    confidence REAL DEFAULT 0.9,
                    applied_count INTEGER DEFAULT 0,
                    created_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_lessons_pattern
                ON lessons (trigger_pattern);
            """)

    def _row_to_lesson(self, row: sqlite3.Row) -> Lesson:
        return Lesson(
            lesson_id=row["lesson_id"],
            trigger_pattern=row["trigger_pattern"],
            category=row["category"],
            observation_error=row["observation_error"],
            learned_solution=row["learned_solution"],
            evidence_run_id=row["evidence_run_id"],
            confidence=row["confidence"],
            applied_count=row["applied_count"],
            created_at=row["created_at"],
        )

    def record_lesson(
        self,
        trigger_pattern: str,
        category: str,
        learned_solution: str,
        observation_error: str | None = None,
        evidence_run_id: str | None = None,
        confidence: float = 0.9,
    ) -> Lesson:
        lesson = Lesson(
            trigger_pattern=trigger_pattern.lower().strip(),
            category=category,
            observation_error=observation_error,
            learned_solution=learned_solution,
            evidence_run_id=evidence_run_id,
            confidence=confidence,
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Check for existing matching lesson to increment confidence or update
            existing = conn.execute(
                """
                SELECT * FROM lessons
                WHERE trigger_pattern = ? AND category = ?
                """,
                (lesson.trigger_pattern, lesson.category),
            ).fetchone()

            if existing:
                new_conf = min(1.0, existing["confidence"] + 0.05)
                conn.execute(
                    """
                    UPDATE lessons
                    SET learned_solution = ?, confidence = ?, evidence_run_id = ?, created_at = ?
                    WHERE lesson_id = ?
                    """,
                    (learned_solution, new_conf, evidence_run_id, now, existing["lesson_id"]),
                )
                logger.info(f"[LessonStore] Updated existing lesson for pattern '{trigger_pattern}'")
                return self._row_to_lesson(
                    conn.execute("SELECT * FROM lessons WHERE lesson_id = ?", (existing["lesson_id"],)).fetchone()
                )

            conn.execute(
                """
                INSERT INTO lessons (
                    lesson_id, trigger_pattern, category, observation_error,
                    learned_solution, evidence_run_id, confidence, applied_count, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    lesson.lesson_id,
                    lesson.trigger_pattern,
                    lesson.category,
                    lesson.observation_error,
                    lesson.learned_solution,
                    lesson.evidence_run_id,
                    lesson.confidence,
                    0,
                    now,
                ),
            )
        logger.info(f"[LessonStore] Recorded new lesson [{category}] for '{trigger_pattern}': {learned_solution}")
        return lesson

    def retrieve_lessons(self, task: str, query: str = "", top_k: int = 5) -> list[Lesson]:
        """
        Retrieve lessons relevant to the task and current runtime context.
        Matches keywords and trigger patterns.
        """
        combined = f"{task} {query}".lower()
        with self._get_connection() as conn:
            rows = conn.execute("SELECT * FROM lessons ORDER BY confidence DESC, applied_count DESC").fetchall()
            matched: list[Lesson] = []
            for r in rows:
                pattern = r["trigger_pattern"].lower()
                # Check if pattern words are in combined query or vice versa
                pattern_words = [w for w in pattern.split() if len(w) > 2]
                if pattern in combined or any(w in combined for w in pattern_words):
                    matched.append(self._row_to_lesson(r))
                    if len(matched) >= top_k:
                        break
            return matched

    def increment_applied(self, lesson_id: str) -> None:
        with self._get_connection() as conn:
            conn.execute(
                "UPDATE lessons SET applied_count = applied_count + 1 WHERE lesson_id = ?",
                (lesson_id,),
            )

    def list_lessons(self, limit: int = 50) -> list[Lesson]:
        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM lessons ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
            return [self._row_to_lesson(r) for r in rows]

    @staticmethod
    def format_lessons(lessons: list[Lesson]) -> str:
        """Format retrieved lessons into actionable markdown context for LLM prompt."""
        if not lessons:
            return ""

        lines = ["\n### 💡 Lessons Learned from Past Executions (Apply directly to save steps):"]
        for les in lessons:
            lines.append(f"- **[{les.category.upper()}]** {les.learned_solution} (Confidence: {les.confidence:.2f})")
        return "\n".join(lines) + "\n"
