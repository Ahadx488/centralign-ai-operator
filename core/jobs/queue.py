from __future__ import annotations

import json
import logging
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from core.jobs.models import Job, JobStatus

logger = logging.getLogger(__name__)


class JobQueue:
    """
    SQLite-backed durable job queue for CentrAlign autonomous agent runs.
    Supports atomic job acquisition, worker heartbeats, cron/scheduled jobs,
    retry limits with backoff, and orphaned job recovery.
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
                CREATE TABLE IF NOT EXISTS jobs (
                    job_id TEXT PRIMARY KEY,
                    run_id TEXT,
                    task TEXT NOT NULL,
                    status TEXT NOT NULL,
                    cron_expression TEXT,
                    next_run_at TEXT,
                    retry_count INTEGER DEFAULT 0,
                    max_retries INTEGER DEFAULT 3,
                    worker_id TEXT,
                    heartbeat_at TEXT,
                    result_data_json TEXT,
                    error TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
            """)
            conn.execute("""
                CREATE INDEX IF NOT EXISTS idx_jobs_status_next
                ON jobs (status, next_run_at);
            """)

    def _row_to_job(self, row: sqlite3.Row) -> Job:
        result_data = None
        if row["result_data_json"]:
            try:
                result_data = json.loads(row["result_data_json"])
            except Exception:
                pass

        return Job(
            job_id=row["job_id"],
            run_id=row["run_id"],
            task=row["task"],
            status=JobStatus(row["status"]),
            cron_expression=row["cron_expression"],
            next_run_at=row["next_run_at"],
            retry_count=row["retry_count"],
            max_retries=row["max_retries"],
            worker_id=row["worker_id"],
            heartbeat_at=row["heartbeat_at"],
            result_data=result_data,
            error=row["error"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
        )

    def enqueue(
        self,
        task: str,
        cron_expression: str | None = None,
        next_run_at: str | None = None,
        run_id: str | None = None,
        max_retries: int = 3,
    ) -> Job:
        job = Job(
            task=task,
            cron_expression=cron_expression,
            next_run_at=next_run_at,
            run_id=run_id,
            max_retries=max_retries,
            status=JobStatus.QUEUED,
        )
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                INSERT INTO jobs (
                    job_id, run_id, task, status, cron_expression, next_run_at,
                    retry_count, max_retries, worker_id, heartbeat_at,
                    result_data_json, error, created_at, updated_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    job.job_id,
                    job.run_id,
                    job.task,
                    job.status.value,
                    job.cron_expression,
                    job.next_run_at,
                    job.retry_count,
                    job.max_retries,
                    job.worker_id,
                    job.heartbeat_at,
                    None,
                    job.error,
                    now,
                    now,
                ),
            )
        logger.info(f"[JobQueue] Enqueued job {job.job_id} for task: {task[:50]}")
        return job

    def get_job(self, job_id: str) -> Job | None:
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            return self._row_to_job(row) if row else None

    def list_jobs(self, status: JobStatus | None = None, limit: int = 50) -> list[Job]:
        with self._get_connection() as conn:
            if status:
                rows = conn.execute(
                    "SELECT * FROM jobs WHERE status = ? ORDER BY created_at DESC LIMIT ?",
                    (status.value, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM jobs ORDER BY created_at DESC LIMIT ?", (limit,)
                ).fetchall()
            return [self._row_to_job(r) for r in rows]

    def acquire_next(self, worker_id: str) -> Job | None:
        """
        Atomically acquire the next eligible QUEUED job (or scheduled job whose next_run_at <= now).
        """
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            # Query eligible job
            row = conn.execute(
                """
                SELECT * FROM jobs
                WHERE status = ?
                  AND (next_run_at IS NULL OR next_run_at <= ?)
                ORDER BY created_at ASC
                LIMIT 1
                """,
                (JobStatus.QUEUED.value, now),
            ).fetchone()

            if not row:
                return None

            job_id = row["job_id"]
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, worker_id = ?, heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND status = ?
                """,
                (JobStatus.RUNNING.value, worker_id, now, now, job_id, JobStatus.QUEUED.value),
            )
            # Re-read
            updated_row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if updated_row and updated_row["worker_id"] == worker_id:
                logger.info(f"[JobQueue] Worker {worker_id} acquired job {job_id}")
                return self._row_to_job(updated_row)
            return None

    def heartbeat(self, job_id: str, worker_id: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET heartbeat_at = ?, updated_at = ?
                WHERE job_id = ? AND worker_id = ? AND status = ?
                """,
                (now, now, job_id, worker_id, JobStatus.RUNNING.value),
            )

    def complete(self, job_id: str, result_data: dict[str, Any] | None = None) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, result_data_json = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (
                    JobStatus.COMPLETED.value,
                    json.dumps(result_data, default=str) if result_data else None,
                    now,
                    job_id,
                ),
            )
        logger.info(f"[JobQueue] Marked job {job_id} as COMPLETED")

    def fail(self, job_id: str, error: str) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row:
                return

            retry_count = row["retry_count"] + 1
            max_retries = row["max_retries"]

            if retry_count <= max_retries:
                # Re-queue for retry
                status = JobStatus.QUEUED.value
                logger.warning(
                    f"[JobQueue] Job {job_id} failed with error '{error}'. Retrying ({retry_count}/{max_retries})."
                )
            else:
                status = JobStatus.FAILED.value
                logger.error(
                    f"[JobQueue] Job {job_id} failed permanently after {max_retries} retries: {error}"
                )

            conn.execute(
                """
                UPDATE jobs
                SET status = ?, retry_count = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (status, retry_count, error, now, job_id),
            )

    def pause(self, job_id: str, reason: str = "") -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self._get_connection() as conn:
            conn.execute(
                """
                UPDATE jobs
                SET status = ?, error = ?, updated_at = ?
                WHERE job_id = ?
                """,
                (JobStatus.PAUSED.value, f"Paused: {reason}" if reason else None, now, job_id),
            )
        logger.info(f"[JobQueue] Marked job {job_id} as PAUSED: {reason}")

    def reclaim_orphaned(self, timeout_seconds: float = 30.0) -> list[Job]:
        """
        Reclaim jobs marked RUNNING whose worker heartbeat has not updated within timeout_seconds.
        Resets them to QUEUED so another worker can resume or pick them up.
        """
        now_dt = datetime.now(timezone.utc)
        reclaimed: list[Job] = []

        with self._get_connection() as conn:
            rows = conn.execute(
                "SELECT * FROM jobs WHERE status = ?", (JobStatus.RUNNING.value,)
            ).fetchall()

            for r in rows:
                heartbeat_str = r["heartbeat_at"] or r["updated_at"]
                hb_dt = datetime.fromisoformat(heartbeat_str)
                delta_sec = (now_dt - hb_dt).total_seconds()

                if delta_sec > timeout_seconds:
                    job_id = r["job_id"]
                    now_str = now_dt.isoformat()
                    conn.execute(
                        """
                        UPDATE jobs
                        SET status = ?, worker_id = NULL, updated_at = ?
                        WHERE job_id = ?
                        """,
                        (JobStatus.QUEUED.value, now_str, job_id),
                    )
                    reclaimed.append(self._row_to_job(r))
                    logger.warning(
                        f"[JobQueue] Reclaimed orphaned job {job_id} (last heartbeat {delta_sec:.1f}s ago)"
                    )

        return reclaimed
