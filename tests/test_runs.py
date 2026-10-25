from __future__ import annotations

import pytest

from core.runs.manager import RunManager
from core.runs.models import RunStatus, StepRecord


@pytest.fixture
def run_manager(tmp_path) -> RunManager:
    db_file = tmp_path / "test_runs.db"
    artifacts_dir = tmp_path / "test_artifacts"
    return RunManager(db_path=db_file, artifacts_dir=artifacts_dir)


def test_run_creation_and_initial_state(run_manager: RunManager):
    run = run_manager.create_run(task="Process invoice from Acme Corp")
    assert run.run_id.startswith("run_")
    assert run.status == RunStatus.CREATED
    assert run.task == "Process invoice from Acme Corp"
    assert run.step_count == 0
    assert run.tokens_used == 0

    transitions = run_manager.get_transitions(run.run_id)
    assert len(transitions) == 1
    assert transitions[0].to_status == RunStatus.CREATED


def test_valid_state_transitions(run_manager: RunManager):
    run = run_manager.create_run(task="Test workflow transitions")
    run_id = run.run_id

    # CREATED -> CLARIFYING
    r1 = run_manager.transition(run_id, RunStatus.CLARIFYING, reason="Clarifying task")
    assert r1.status == RunStatus.CLARIFYING

    # CLARIFYING -> PLANNING
    r2 = run_manager.transition(run_id, RunStatus.PLANNING, reason="Creating plan")
    assert r2.status == RunStatus.PLANNING

    # PLANNING -> RUNNING
    r3 = run_manager.transition(run_id, RunStatus.RUNNING, reason="Starting steps")
    assert r3.status == RunStatus.RUNNING

    # RUNNING -> WAITING_APPROVAL
    r4 = run_manager.transition(run_id, RunStatus.WAITING_APPROVAL, reason="Approval needed")
    assert r4.status == RunStatus.WAITING_APPROVAL

    # WAITING_APPROVAL -> RUNNING
    r5 = run_manager.transition(run_id, RunStatus.RUNNING, reason="Approved by manager")
    assert r5.status == RunStatus.RUNNING

    # RUNNING -> VERIFYING
    r6 = run_manager.transition(run_id, RunStatus.VERIFYING, reason="All intents complete")
    assert r6.status == RunStatus.VERIFYING

    # VERIFYING -> COMPLETED
    r7 = run_manager.transition(run_id, RunStatus.COMPLETED, reason="Verification confirmed")
    assert r7.status == RunStatus.COMPLETED

    transitions = run_manager.get_transitions(run_id)
    assert len(transitions) == 8
    assert [t.to_status for t in transitions] == [
        RunStatus.CREATED,
        RunStatus.CLARIFYING,
        RunStatus.PLANNING,
        RunStatus.RUNNING,
        RunStatus.WAITING_APPROVAL,
        RunStatus.RUNNING,
        RunStatus.VERIFYING,
        RunStatus.COMPLETED,
    ]


def test_invalid_state_transitions_rejected(run_manager: RunManager):
    run = run_manager.create_run(task="Invalid transition test")
    run_id = run.run_id

    # CREATED cannot jump directly to COMPLETED
    with pytest.raises(ValueError, match="Invalid transition from CREATED to COMPLETED"):
        run_manager.transition(run_id, RunStatus.COMPLETED)

    # CREATED cannot jump to WAITING_APPROVAL
    with pytest.raises(ValueError, match="Invalid transition from CREATED to WAITING_APPROVAL"):
        run_manager.transition(run_id, RunStatus.WAITING_APPROVAL)


def test_update_run_and_working_memory(run_manager: RunManager):
    run = run_manager.create_run(task="Memory test")
    run_id = run.run_id

    goal_data = {"outcome": "Pay bill", "criteria": ["bill paid"]}
    plan_data = {"intents": [{"id": "i1", "description": "find bill"}]}
    mem_data = {"vendor": "Globex Systems", "amount": 1200.50}

    updated = run_manager.update_run(
        run_id,
        goal=goal_data,
        plan=plan_data,
        working_memory=mem_data,
    )
    assert updated.goal == goal_data
    assert updated.plan == plan_data
    assert updated.working_memory == mem_data

    # Reload from DB
    loaded = run_manager.get_run(run_id)
    assert loaded is not None
    assert loaded.working_memory["vendor"] == "Globex Systems"
    assert loaded.working_memory["amount"] == 1200.50


def test_step_audit_log(run_manager: RunManager):
    run = run_manager.create_run(task="Audit log test")
    run_id = run.run_id

    step1 = StepRecord(
        run_id=run_id,
        step_index=1,
        intent_id="intent_1",
        thought="Navigating to portal",
        tool_name="open_url",
        tool_args={"url": "http://localhost:8001/portal"},
        observation_summary="Page loaded successfully",
        ok=True,
        tokens=150,
        duration_ms=45.2,
    )
    step2 = StepRecord(
        run_id=run_id,
        step_index=2,
        intent_id="intent_1",
        thought="Logging in",
        tool_name="click",
        tool_args={"target": "e3"},
        observation_summary="Logged in",
        ok=True,
        tokens=120,
        duration_ms=88.5,
    )

    run_manager.record_step(run_id, step1)
    run_manager.record_step(run_id, step2)

    steps = run_manager.get_steps(run_id)
    assert len(steps) == 2
    assert steps[0].tool_name == "open_url"
    assert steps[0].tokens == 150
    assert steps[1].tool_name == "click"
    assert steps[1].tokens == 120

    # Verify run totals updated
    updated_run = run_manager.get_run(run_id)
    assert updated_run is not None
    assert updated_run.step_count == 2
    assert updated_run.tokens_used == 270


def test_artifacts_saving_and_retrieval(run_manager: RunManager):
    run = run_manager.create_run(task="Artifacts test")
    run_id = run.run_id

    large_snapshot = "<html>" + "<div>content</div>" * 500 + "</html>"
    artifact = run_manager.save_artifact(
        run_id=run_id,
        name="page_snapshot",
        content=large_snapshot,
        summary="Portal DOM snapshot",
        mime_type="text/html",
    )

    assert artifact.artifact_id.startswith("art_")
    assert artifact.size_bytes == len(large_snapshot)
    assert artifact.mime_type == "text/html"

    # Verify retrieval
    arts = run_manager.get_artifacts(run_id)
    assert len(arts) == 1
    assert arts[0].name == "page_snapshot"
    assert arts[0].summary == "Portal DOM snapshot"
