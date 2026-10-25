from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING, Any
from uuid import uuid4

if TYPE_CHECKING:
    from core.agent.executor import AgentExecutor
from core.jobs.models import Job
from core.jobs.queue import JobQueue
from core.runs.manager import RunManager
from core.runs.models import RunRecord, RunStatus

logger = logging.getLogger(__name__)


class JobWorker:
    """
    Background worker that continuously pulls jobs from the JobQueue,
    maintains heartbeats, executes autonomous agent runs, and provides
    durable crash recovery.
    """

    def __init__(
        self,
        queue: JobQueue,
        run_manager: RunManager,
        executor_factory: Any = None,
        worker_id: str | None = None,
        heartbeat_interval: float = 2.0,
    ) -> None:
        self.queue = queue
        self.run_manager = run_manager
        self.executor_factory = executor_factory
        self.worker_id = worker_id or f"worker_{uuid4().hex[:8]}"
        self.heartbeat_interval = heartbeat_interval
        self.is_running = False
        self._current_job: Job | None = None
        self._heartbeat_task: asyncio.Task[None] | None = None

    async def _heartbeat_loop(self) -> None:
        """Periodically update worker heartbeat while executing a job."""
        while self.is_running and self._current_job:
            try:
                self.queue.heartbeat(self._current_job.job_id, self.worker_id)
            except Exception as e:
                logger.warning(f"[Worker {self.worker_id}] Heartbeat error: {e}")
            await asyncio.sleep(self.heartbeat_interval)

    async def run_once(self) -> bool:
        """
        Poll and execute a single job from the queue. Returns True if a job was processed.
        """
        # First reclaim any orphaned jobs
        self.queue.reclaim_orphaned(timeout_seconds=15.0)

        job = self.queue.acquire_next(self.worker_id)
        if not job:
            return False

        self._current_job = job
        logger.info(f"[Worker {self.worker_id}] Processing job {job.job_id} for task: {job.task[:60]}")

        # Start heartbeat
        self.is_running = True
        self._heartbeat_task = asyncio.create_task(self._heartbeat_loop())

        try:
            executor: AgentExecutor
            if callable(self.executor_factory):
                executor = self.executor_factory()
            else:
                executor = self.executor_factory

            if not executor:
                raise RuntimeError("No executor available to process job")

            # Check if this job has an existing run to resume or needs a fresh run
            run_rec: RunRecord
            if job.run_id and self.run_manager.get_run(job.run_id):
                run_rec = await resume_interrupted_run(
                    job.run_id,
                    executor=executor,
                    run_manager=self.run_manager,
                )
            else:
                run_rec = await executor.run(job.task)
                # Link run_id to job
                with self.queue._get_connection() as conn:
                    conn.execute("UPDATE jobs SET run_id = ? WHERE job_id = ?", (run_rec.run_id, job.job_id))

            # Update job status according to run outcome
            if run_rec.status == RunStatus.COMPLETED:
                self.queue.complete(job.job_id, result_data={"run_id": run_rec.run_id, "status": "COMPLETED"})
            elif run_rec.status in {RunStatus.WAITING_APPROVAL, RunStatus.WAITING_USER}:
                self.queue.pause(job.job_id, reason=f"Run waiting on {run_rec.status.value}")
            elif run_rec.status in {RunStatus.FAILED, RunStatus.ESCALATED}:
                self.queue.fail(job.job_id, error=run_rec.error or f"Run entered {run_rec.status.value}")

            return True

        except Exception as e:
            logger.error(f"[Worker {self.worker_id}] Fatal job failure on {job.job_id}: {e}", exc_info=True)
            self.queue.fail(job.job_id, error=str(e))
            return True

        finally:
            self._current_job = None
            if self._heartbeat_task and not self._heartbeat_task.done():
                self._heartbeat_task.cancel()
                try:
                    await self._heartbeat_task
                except asyncio.CancelledError:
                    pass


async def resume_interrupted_run(
    run_id: str,
    executor: AgentExecutor,
    run_manager: RunManager | None = None,
) -> RunRecord:
    """
    Pick up an orphaned or crashed run from the exact last persisted SQLite step,
    leveraging idempotency keys to ensure no write action is duplicated.
    """
    rm = run_manager or executor.run_manager
    run = rm.get_run(run_id)
    if not run:
        raise ValueError(f"Run {run_id} not found for crash resumption")

    steps = rm.get_steps(run_id)
    last_step_idx = len(steps)
    logger.info(
        f"[Crash Recovery] Resuming interrupted run {run_id} (Status: {run.status.value}, "
        f"persisted steps: {last_step_idx})"
    )

    if run.status == RunStatus.COMPLETED:
        logger.info(f"[Crash Recovery] Run {run_id} is already in COMPLETED state")
        return run

    # Transition run back to RUNNING if it was orphaned mid-execution
    if run.status != RunStatus.RUNNING:
        rm.transition(
            run_id,
            RunStatus.RUNNING,
            reason=f"Worker recovering orphaned run from step {last_step_idx}",
            force=True,
        )

    # Invoke resume_interrupted on executor
    return await executor.resume_interrupted(run_id)
