from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Any
from uuid import uuid4

import yaml
from fastapi import APIRouter, Body, Form, Query, Request, Response
from fastapi.responses import HTMLResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from core.agent.executor import AgentExecutor
from core.agent.reflector import Reflector
from core.llm.client import get_llm_client
from core.policy.approvals import ApprovalInbox, ApprovalStatus
from core.runs.manager import RunManager
from core.runs.models import RunRecord
from core.tools import register_default_tools, tool_registry
from core.verify.verifier import IndependentVerifier
from env.faults.manager import fault_manager

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ui", tags=["Dashboard"])

TEMPLATES_DIR = os.path.join(os.path.dirname(__file__), "templates")
templates = Jinja2Templates(directory=TEMPLATES_DIR)

RUNS_DB_PATH = "artifacts/runs.db"
ARTIFACTS_DIR = "artifacts"

# Global registry of active in-memory executors for paused runs
active_executors: dict[str, AgentExecutor] = {}
run_tasks: dict[str, asyncio.Task[Any]] = {}


def get_run_manager() -> RunManager:
    return RunManager(db_path=RUNS_DB_PATH, artifacts_dir=ARTIFACTS_DIR)


def get_active_llm() -> Any:
    if "PYTEST_CURRENT_TEST" in os.environ:
        from core.agent.models import AgentAction
        from core.llm.fake import FakeLLM
        from core.llm.models import LLMResponse

        mock_llm = FakeLLM()
        mock_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Mock step", is_finish=True, finish_summary="Mock completed")))
        return mock_llm
    return get_llm_client()


def load_evidence_bundle(run_id: str) -> dict[str, Any] | None:
    bundle_path = os.path.join(ARTIFACTS_DIR, "runs", run_id, "evidence_bundle.json")
    if os.path.exists(bundle_path):
        try:
            with open(bundle_path) as f:
                return json.load(f)
        except Exception as e:
            logger.warning(f"Failed to load evidence bundle for {run_id}: {e}")
    return None


@router.get("", response_class=HTMLResponse)
async def dashboard_index(request: Request, run_id: str | None = None) -> Response:
    """Render the main CentrAlign Agent Dashboard."""
    rm = get_run_manager()
    runs = rm.list_runs(limit=30)

    active_run: RunRecord | None = None
    if run_id:
        active_run = rm.get_run(run_id)
    elif runs:
        active_run = runs[0]

    steps = rm.get_steps(active_run.run_id) if active_run else []
    evidence_bundle = load_evidence_bundle(active_run.run_id) if active_run else None
    evidence_md_path = os.path.join(ARTIFACTS_DIR, "runs", active_run.run_id, "evidence_bundle.md") if active_run else ""

    return templates.TemplateResponse(
        request=request,
        name="dashboard.html",
        context={
            "runs": runs,
            "active_run": active_run,
            "run": active_run,
            "steps": steps,
            "verification_bundle": evidence_bundle,
            "evidence_file_exists": os.path.exists(evidence_md_path),
        },
    )


@router.get("/runs/{run_id}/live", response_class=HTMLResponse)
async def live_run_partial(request: Request, run_id: str) -> Response:
    """Return partial HTML for HTMX polling of live run progress."""
    rm = get_run_manager()
    run = rm.get_run(run_id)
    steps = rm.get_steps(run_id) if run else []
    evidence_bundle = load_evidence_bundle(run_id) if run else None
    evidence_md_path = os.path.join(ARTIFACTS_DIR, "runs", run_id, "evidence_bundle.md")

    return templates.TemplateResponse(
        request=request,
        name="live_run.html",
        context={
            "run": run,
            "steps": steps,
            "verification_bundle": evidence_bundle,
            "evidence_file_exists": os.path.exists(evidence_md_path),
        },
    )


@router.post("/launch")
async def launch_run(
    task_body: dict[str, Any] = Body(default_factory=dict),
    scenario: int | str | None = Query(default=None),
) -> dict[str, Any]:
    """Launch a new goal or scenario in the background."""
    register_default_tools(tool_registry)
    run_manager = get_run_manager()
    verifier = IndependentVerifier(db_path="env/company.db", artifacts_dir=ARTIFACTS_DIR)

    task_str = task_body.get("task", "")
    if "PYTEST_CURRENT_TEST" in os.environ:
        from core.agent.models import AgentAction, Goal, Intent
        from core.agent.planner import InitialPlanOutput
        from core.llm.fake import FakeLLM
        from core.llm.models import LLMResponse

        llm: Any = FakeLLM()
        llm.queue_response(LLMResponse(parsed=Goal(outcome="Mock UI task"), provider="fake"))
        llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="step", expected_outcome="done")]), provider="fake"))
        llm.queue_response(LLMResponse(parsed=AgentAction(thought="Ask question", tool="ask_human", args={"question": "Clarify?", "options": ["A", "B"]})))
        llm.queue_response(LLMResponse(parsed=AgentAction(thought="Finished", is_finish=True, finish_summary="Done")))
    else:
        llm = get_llm_client()

    if scenario is not None:
        sc_num = int(scenario) if str(scenario).isdigit() else scenario
        scenarios_file = Path("company/scenarios.yaml")
        sc_data: dict[Any, Any] = {}
        if scenarios_file.exists():
            with open(scenarios_file) as sf:
                raw_loaded = yaml.safe_load(sf)
                if isinstance(raw_loaded, dict):
                    sc_data = raw_loaded
        sc_info: dict[str, Any] = sc_data.get(sc_num) or sc_data.get(str(sc_num)) or {}
        task_str = task_str or sc_info.get("task", "")
        sc_faults = sc_info.get("faults")
        if sc_faults:
            fault_manager.update_faults(sc_faults)

    run_id = f"run_{uuid4().hex[:12]}"
    executor = AgentExecutor(
        llm_client=llm,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),
        verifier=verifier,
        max_steps=30,
    )
    active_executors[run_id] = executor

    async def _run_worker():
        try:
            await executor.run(task_str, run_id=run_id)
        except Exception as e:
            logger.error(f"Error in background agent run {run_id}: {e}")

    task_obj = asyncio.create_task(_run_worker())
    run_tasks[run_id] = task_obj

    # Brief delay so run record is initialized in SQLite
    await asyncio.sleep(0.1)

    return {"ok": True, "run_id": run_id, "task": task_str}


@router.post("/runs/{run_id}/resume")
async def resume_run(run_id: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Resume a paused run after human clarification."""
    human_input = body.get("human_input", "")
    executor = active_executors.get(run_id)
    if not executor:
        rm = get_run_manager()
        llm = get_active_llm()
        executor = AgentExecutor(
            llm_client=llm,
            run_manager=rm,
            registry=tool_registry,
            reflector=Reflector(None),
            verifier=IndependentVerifier(db_path="env/company.db", artifacts_dir=ARTIFACTS_DIR),
        )
        active_executors[run_id] = executor

    async def _resume_worker():
        try:
            await executor.resume(run_id, human_input=human_input)
        except Exception as e:
            logger.error(f"Error resuming run {run_id}: {e}")

    asyncio.create_task(_resume_worker())
    await asyncio.sleep(0.1)

    return {"ok": True, "resumed": run_id, "human_input": human_input}


@router.post("/runs/{run_id}/approve")
async def approve_run(run_id: str, body: dict[str, Any] = Body(default_factory=dict)) -> dict[str, Any]:
    """Resume a paused approval run with approval decision."""
    approved = bool(body.get("approved", True))
    executor = active_executors.get(run_id)
    if not executor:
        rm = get_run_manager()
        llm = get_active_llm()
        executor = AgentExecutor(
            llm_client=llm,
            run_manager=rm,
            registry=tool_registry,
            reflector=Reflector(None),
            verifier=IndependentVerifier(db_path="env/company.db", artifacts_dir=ARTIFACTS_DIR),
        )
        active_executors[run_id] = executor

    async def _approve_worker():
        try:
            await executor.resume(run_id, approval_granted=approved)
        except Exception as e:
            logger.error(f"Error in approval resume for {run_id}: {e}")

    asyncio.create_task(_approve_worker())
    await asyncio.sleep(0.1)

    return {"ok": True, "run_id": run_id, "approved": approved}


@router.get("/approvals", response_class=HTMLResponse)
async def approvals_inbox_view(request: Request) -> Response:
    """Render the Human-in-the-Loop Approvals Inbox."""
    inbox = ApprovalInbox(db_path=RUNS_DB_PATH)
    inbox.check_timeouts()  # Auto-escalate timed out requests
    pending = inbox.get_pending()
    all_reqs = inbox.list_requests(limit=40)
    history = [r for r in all_reqs if r.status != ApprovalStatus.PENDING]

    return templates.TemplateResponse(
        request=request,
        name="approvals.html",
        context={
            "pending": pending,
            "history": history,
        },
    )


@router.post("/approvals/{approval_id}/approve")
async def approve_request_action(approval_id: str) -> Response:
    """Approve an action from the Approvals Inbox and resume the corresponding agent run."""
    inbox = ApprovalInbox(db_path=RUNS_DB_PATH)
    req = inbox.decide(approval_id, decision="APPROVE", decided_by="human_operator")
    run_id = req.run_id

    executor = active_executors.get(run_id)
    if not executor:
        rm = get_run_manager()
        llm = get_active_llm()
        executor = AgentExecutor(
            llm_client=llm,
            run_manager=rm,
            registry=tool_registry,
            reflector=Reflector(None),
            verifier=IndependentVerifier(db_path="env/company.db", artifacts_dir=ARTIFACTS_DIR),
        )
        active_executors[run_id] = executor

    async def _resume_worker():
        try:
            await executor.resume(run_id, approval_granted=True)
        except Exception as e:
            logger.error(f"Error resuming approved run {run_id}: {e}")

    asyncio.create_task(_resume_worker())
    await asyncio.sleep(0.1)
    return RedirectResponse(url="/ui/approvals", status_code=303)


@router.post("/approvals/{approval_id}/reject")
async def reject_request_action(approval_id: str) -> Response:
    """Reject an action from the Approvals Inbox and transition the agent run to ESCALATED."""
    inbox = ApprovalInbox(db_path=RUNS_DB_PATH)
    req = inbox.decide(approval_id, decision="REJECT", decided_by="human_operator")
    run_id = req.run_id

    executor = active_executors.get(run_id)
    if not executor:
        rm = get_run_manager()
        llm = get_active_llm()
        executor = AgentExecutor(
            llm_client=llm,
            run_manager=rm,
            registry=tool_registry,
            reflector=Reflector(None),
            verifier=IndependentVerifier(db_path="env/company.db", artifacts_dir=ARTIFACTS_DIR),
        )
        active_executors[run_id] = executor

    async def _reject_worker():
        try:
            await executor.resume(run_id, approval_granted=False)
        except Exception as e:
            logger.error(f"Error rejecting run {run_id}: {e}")

    asyncio.create_task(_reject_worker())
    await asyncio.sleep(0.1)
    return RedirectResponse(url="/ui/approvals", status_code=303)


@router.post("/approvals/{approval_id}/edit")
async def edit_and_approve_request_action(
    approval_id: str,
    edited_args_json: str = Form(...),
) -> Response:
    """Edit tool arguments and approve the modified action."""
    inbox = ApprovalInbox(db_path=RUNS_DB_PATH)
    try:
        edited_args = json.loads(edited_args_json)
    except Exception as e:
        logger.warning(f"Could not parse edited JSON ({e}); passing raw dict: {edited_args_json}")
        edited_args = {"raw_edit": edited_args_json}

    req = inbox.decide(
        approval_id,
        decision="EDIT_APPROVE",
        edited_args=edited_args,
        decided_by="human_operator",
        notes="Edited arguments by operator",
    )
    run_id = req.run_id

    executor = active_executors.get(run_id)
    if not executor:
        rm = get_run_manager()
        llm = get_active_llm()
        executor = AgentExecutor(
            llm_client=llm,
            run_manager=rm,
            registry=tool_registry,
            reflector=Reflector(None),
            verifier=IndependentVerifier(db_path="env/company.db", artifacts_dir=ARTIFACTS_DIR),
        )
        active_executors[run_id] = executor

    async def _edit_approve_worker():
        try:
            await executor.resume(run_id, approval_granted=True, edited_args=edited_args)
        except Exception as e:
            logger.error(f"Error resuming edited-approved run {run_id}: {e}")

    asyncio.create_task(_edit_approve_worker())
    await asyncio.sleep(0.1)
    return RedirectResponse(url="/ui/approvals", status_code=303)


@router.get("/runs/{run_id}/evidence", response_class=PlainTextResponse)
async def view_evidence(run_id: str) -> Response:
    """Return raw evidence bundle markdown report."""
    md_path = os.path.join(ARTIFACTS_DIR, "runs", run_id, "evidence_bundle.md")
    if os.path.exists(md_path):
        with open(md_path) as f:
            return PlainTextResponse(f.read())
    return PlainTextResponse(f"Evidence report not found for run {run_id}", status_code=404)
