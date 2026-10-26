from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.agent.executor import AgentExecutor
from core.agent.models import AgentAction, Goal, Intent
from core.agent.planner import InitialPlanOutput
from core.agent.reflector import Reflector
from core.llm.fake import FakeLLM
from core.llm.models import LLMResponse
from core.runs.manager import RunManager
from core.runs.models import RunStatus
from core.tools import register_default_tools
from core.tools.connectors import ConnectorRegistry
from core.tools.registry import ToolRegistry
from env.db import init_db
from env.seed.generator import seed_company_data
from tests.conftest import BASE_URL


def create_generalization_task_1_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Generalization Task 1: Close all tickets assigned to me that were resolved last week and email summary to manager."""
    llm = FakeLLM()
    llm.queue_response(
        LLMResponse(
            parsed=Goal(
                outcome="Close all resolved service tickets assigned to me and email closure summary to manager.",
                success_criteria=[
                    "Service desk tickets TICK-102 and TICK-103 closed",
                    "Summary email dispatched to manager via corporate mailbox",
                ],
                is_blocking=False,
            ),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(id="intent_1", description="Log in to service desk and list resolved tickets", expected_outcome="Tickets identified"),
                    Intent(id="intent_2", description="Update status of resolved tickets to closed", expected_outcome="Tickets closed"),
                    Intent(id="intent_3", description="Send summary email to manager", expected_outcome="Email sent"),
                ]
            ),
            provider="fake",
        )
    )
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to tickets login", tool="browser_open_url", args={"url": f"{base_url}/tickets/login"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering username", tool="browser_type_text", args={"target": "#username", "text": "operator"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering password", tool="browser_type_text", args={"target": "#password", "text": "tickets_pass123"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting login", tool="browser_click", args={"target": "#tickets-submit-btn"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to TICK-102", tool="browser_open_url", args={"url": f"{base_url}/tickets/TICK-102"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Closing TICK-102", tool="browser_select_option", args={"target": "#status", "value": "closed"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting update", tool="browser_click", args={"target": "#save-ticket-btn"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to TICK-103", tool="browser_open_url", args={"url": f"{base_url}/tickets/TICK-103"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Closing TICK-103", tool="browser_select_option", args={"target": "#status", "value": "closed"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting update", tool="browser_click", args={"target": "#save-ticket-btn"})))
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Sending summary email to manager Sneha Patel",
                tool="mail_send",
                args={
                    "recipient": "sneha.patel@company.com",
                    "subject": "Weekly Resolved Tickets Closure Summary",
                    "body": "Dear Sneha,\n\nAll resolved tickets assigned for last week have been reviewed and closed:\n- TICK-102: Reset vendor portal password for Globex (closed)\n- TICK-103: Upgrade database storage (closed)\n\nRegards,\nTechnical Operator",
                },
            )
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Finished ticket triage and dispatching report.",
                is_finish=True,
                finish_summary="Successfully closed resolved tickets TICK-102 and TICK-103 and emailed summary report to manager Sneha Patel.",
            )
        )
    )
    return llm


def create_generalization_task_2_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Generalization Task 2: Reconcile vendor statement CSV in file share against bills in ERP and list mismatches."""
    llm = FakeLLM()
    llm.queue_response(
        LLMResponse(
            parsed=Goal(
                outcome="Reconcile vendor statement CSV in the file share against bills in the ERP and report mismatches.",
                success_criteria=[
                    "Vendor statement CSV parsed from file share",
                    "Bills in ERP cross-referenced against statement items",
                    "Mismatches and missing records identified",
                ],
                is_blocking=False,
            ),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(id="intent_1", description="Read vendor statement CSV from file share", expected_outcome="Statement rows loaded"),
                    Intent(id="intent_2", description="Query Acme ERP bills", expected_outcome="ERP ledger loaded"),
                    Intent(id="intent_3", description="Compare records and compile mismatch list", expected_outcome="Mismatches identified"),
                ]
            ),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Reading vendor statement CSV from file share",
                tool="files_read_file",
                args={"path": "./env/fileshare/data/AcmeCorp_Oct2026_statement.csv"},
            )
        )
    )
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to Acme ERP login", tool="browser_open_url", args={"url": f"{base_url}/erp/login"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering username", tool="browser_type_text", args={"target": "#username", "text": "admin"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering password", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Logging into ERP", tool="browser_click", args={"target": "#erp-submit-btn"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Viewing ERP bills register", tool="browser_snapshot", args={})))
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Compiling reconciliation discrepancies between statement CSV and ERP",
                is_finish=True,
                finish_summary=(
                    "Vendor statement reconciliation complete:\n"
                    "1. INV-ACME-101: MATCHED ($34,500.00, Billed)\n"
                    "2. INV-ACME-102: AMOUNT_MISMATCH (Statement indicates INR 12,500.00, but ERP register shows INR 64,977.03)\n"
                    "3. INV-ACME-107: MISSING_IN_ERP (Statement lists USD 68,400.00 pending, absent in ERP bills register)"
                ),
            )
        )
    )
    return llm


def create_generalization_task_3_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Generalization Task 3: Onboard new hire Priya: create the HR ticket, add her to the access sheet, and confirm."""
    llm = FakeLLM()
    llm.queue_response(
        LLMResponse(
            parsed=Goal(
                outcome="Onboard new hire Priya: create the HR ticket, add her to the access sheet, and confirm.",
                success_criteria=[
                    "HR onboarding ticket created in Service Desk",
                    "New hire added to system access control sheet",
                    "Confirmation reported",
                ],
                is_blocking=False,
            ),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(id="intent_1", description="Log in to Service Desk and create HR onboarding ticket", expected_outcome="Ticket created"),
                    Intent(id="intent_2", description="Update system access control sheet in file share", expected_outcome="Access matrix updated"),
                    Intent(id="intent_3", description="Confirm completion", expected_outcome="Task confirmed"),
                ]
            ),
            provider="fake",
        )
    )
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to tickets login", tool="browser_open_url", args={"url": f"{base_url}/tickets/login"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering username", tool="browser_type_text", args={"target": "#username", "text": "operator"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering password", tool="browser_type_text", args={"target": "#password", "text": "tickets_pass123"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Logging in", tool="browser_click", args={"target": "button[type='submit']"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Opening new ticket form", tool="browser_open_url", args={"url": f"{base_url}/tickets/new"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering title", tool="browser_type_text", args={"target": "#title", "text": "Onboard new hire Priya Sharma"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering description", tool="browser_type_text", args={"target": "#description", "text": "Provision employee account, issue hardware, and configure system permissions."})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering assignee", tool="browser_type_text", args={"target": "#assignee", "text": "Deepak Sharma"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting ticket", tool="browser_click", args={"target": "button[type='submit']"})))
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Reading current access matrix", tool="files_read_file", args={"path": "./env/fileshare/data/system_access_list.csv"})))
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Appending Priya Sharma to access matrix",
                tool="files_write_file",
                args={
                    "path": "./env/fileshare/data/system_access_list.csv",
                    "content": (
                        "Employee Name,Role,Department,System Access,Status,Date Added\n"
                        "Sneha Patel,IT Administrator,IT,\"ERP;Tickets;Portal\",Active,2026-01-15\n"
                        "Arjun Verma,Finance Manager,Finance,\"ERP;Portal\",Active,2026-02-01\n"
                        "Deepak Sharma,People Operations Lead,HR,\"Tickets\",Active,2026-03-10\n"
                        "Priya Sharma,Software Engineer,Engineering,\"ERP;Tickets\",Active,2026-10-25\n"
                    ),
                },
            )
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Confirming onboarding completion",
                is_finish=True,
                finish_summary="Successfully onboarded new hire Priya Sharma: created HR Service Desk ticket and recorded credentials in system_access_list.csv.",
            )
        )
    )
    return llm


@pytest.fixture(autouse=True)
def reset_database() -> None:
    init_db()
    seed_company_data()


def test_connector_spec_discovery_and_registry() -> None:
    """Test that all declarative connectors in company/connectors/*.yaml are loaded."""
    reg = ConnectorRegistry(connectors_dir="company/connectors")
    connectors = reg.list_connectors()
    assert len(connectors) >= 4

    sys_keys = {c.system_key for c in connectors}
    assert "ticketing" in sys_keys
    assert "erp" in sys_keys
    assert "mailbox" in sys_keys
    assert "fileshare" in sys_keys

    ticket_conn = reg.get("ticketing")
    assert ticket_conn is not None
    assert ticket_conn.auth is not None
    assert ticket_conn.auth.username_field == "username"
    assert len(ticket_conn.endpoints) >= 3


@pytest.mark.asyncio
async def test_generalization_task_1_ticketing_and_email(tmp_path: Path) -> None:
    """
    Generalization Task 1:
    'Close all tickets assigned to me that were resolved last week and email the summary to my manager.'
    Driven entirely by company/procedures/ticket_triage.md and company/connectors/ticketing.yaml.
    """
    run_manager = RunManager(db_path=tmp_path / "runs.db", artifacts_dir=tmp_path / "artifacts")
    registry = ToolRegistry()
    register_default_tools(registry)

    llm = create_generalization_task_1_llm(base_url=BASE_URL)
    executor = AgentExecutor(
        llm_client=llm,
        run_manager=run_manager,
        registry=registry,
        company_dir="company",
        reflector=Reflector(None),
    )

    task = "Close all tickets assigned to me that were resolved last week and email the summary to my manager."
    run = await executor.run(task)

    assert run.status == RunStatus.COMPLETED

    # Verify directly against company database
    conn = sqlite3.connect("env/company.db")
    conn.row_factory = sqlite3.Row
    with conn:
        # 1. Tickets TICK-102 and TICK-103 must be closed
        t102 = conn.execute("SELECT status FROM tickets WHERE ticket_number = 'TICK-102'").fetchone()
        t103 = conn.execute("SELECT status FROM tickets WHERE ticket_number = 'TICK-103'").fetchone()
        assert t102 is not None and t102["status"] == "closed"
        assert t103 is not None and t103["status"] == "closed"

        # 2. Email dispatched to manager Sneha Patel
        email = conn.execute("SELECT * FROM emails WHERE recipient = 'sneha.patel@company.com' ORDER BY id DESC LIMIT 1").fetchone()
        assert email is not None
        assert "TICK-102" in email["body"]
        assert "TICK-103" in email["body"]


@pytest.mark.asyncio
async def test_generalization_task_2_statement_csv_reconciliation(tmp_path: Path) -> None:
    """
    Generalization Task 2:
    'Reconcile the vendor statement CSV in the file share against bills in the ERP and list mismatches.'
    Driven entirely by company/procedures/vendor_reconciliation.md.
    """
    run_manager = RunManager(db_path=tmp_path / "runs.db", artifacts_dir=tmp_path / "artifacts")
    registry = ToolRegistry()
    register_default_tools(registry)

    llm = create_generalization_task_2_llm(base_url=BASE_URL)
    executor = AgentExecutor(
        llm_client=llm,
        run_manager=run_manager,
        registry=registry,
        company_dir="company",
        reflector=Reflector(None),
    )

    task = "Reconcile the vendor statement CSV in the file share against bills in the ERP and list mismatches."
    run = await executor.run(task)

    assert run.status == RunStatus.COMPLETED

    steps = run_manager.get_steps(run.run_id)
    finish_step = steps[-1]
    # Verify the finish summary correctly identified the discrepancies
    summary = finish_step.observation_summary
    assert "INV-ACME-107" in summary
    assert "INV-ACME-102" in summary


@pytest.mark.asyncio
async def test_generalization_task_3_hr_employee_onboarding(tmp_path: Path) -> None:
    """
    Generalization Task 3:
    'Onboard new hire Priya: create the HR ticket, add her to the access sheet, and confirm.'
    Driven entirely by company/procedures/hr_onboarding.md.
    """
    run_manager = RunManager(db_path=tmp_path / "runs.db", artifacts_dir=tmp_path / "artifacts")
    registry = ToolRegistry()
    register_default_tools(registry)

    llm = create_generalization_task_3_llm(base_url=BASE_URL)
    executor = AgentExecutor(
        llm_client=llm,
        run_manager=run_manager,
        registry=registry,
        company_dir="company",
        reflector=Reflector(None),
    )

    task = "Onboard new hire Priya: create the HR ticket, add her to the access sheet, and confirm."
    run = await executor.run(task)

    assert run.status == RunStatus.COMPLETED

    # 1. Verify ticket created in database
    conn = sqlite3.connect("env/company.db")
    conn.row_factory = sqlite3.Row
    with conn:
        priya_ticket = conn.execute("SELECT * FROM tickets WHERE title LIKE '%Priya%'").fetchone()
        assert priya_ticket is not None
        assert "Priya" in priya_ticket["title"]

    # 2. Verify access sheet in fileshare updated
    access_csv = Path("./env/fileshare/data/system_access_list.csv").read_text(encoding="utf-8")
    assert "Priya Sharma" in access_csv
    assert "Software Engineer" in access_csv


def test_strict_rule_zero_task_specific_branches_in_core_agent() -> None:
    """
    Strict Requirement: Verify there are ZERO task-specific branches or shortcuts
    in core/agent/ (no hardcoded 'if task == ...' or 'if invoice' or 'if priya').
    All general task execution is driven dynamically by company context and connectors.
    """
    agent_dir = Path("core/agent")
    prohibited_keywords = [
        "task == 'invoice'",
        'task == "invoice"',
        "task == 'ticket'",
        'task == "ticket"',
        "task == 'reconciliation'",
        'task == "reconciliation"',
        "task == 'onboard'",
        'task == "onboard"',
        "priya",
        "acme corp ltd",
    ]

    for py_file in agent_dir.glob("*.py"):
        content = py_file.read_text(encoding="utf-8").lower()
        for kw in prohibited_keywords:
            assert kw not in content, f"Violation of Generalization Rule: Hardcoded task shortcut '{kw}' found in {py_file}"
