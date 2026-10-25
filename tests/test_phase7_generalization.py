from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.agent.executor import AgentExecutor
from core.agent.reflector import Reflector
from core.runs.manager import RunManager
from core.runs.models import RunStatus
from core.scenarios import (
    create_generalization_task_1_llm,
    create_generalization_task_2_llm,
    create_generalization_task_3_llm,
)
from core.tools import register_default_tools
from core.tools.connectors import ConnectorRegistry
from core.tools.registry import ToolRegistry
from env.db import init_db
from env.seed.generator import seed_company_data
from tests.conftest import BASE_URL


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
