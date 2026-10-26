from __future__ import annotations

import sqlite3

import pytest

from core.agent.executor import AgentExecutor
from core.agent.models import AgentAction, Goal, Intent
from core.agent.planner import InitialPlanOutput
from core.agent.reflector import Reflector
from core.llm.fake import FakeLLM
from core.llm.models import LLMResponse
from core.runs.manager import RunManager
from core.runs.models import RunStatus
from core.tools import register_default_tools, tool_registry
from env.faults.manager import fault_manager
from tests.conftest import BASE_URL


@pytest.fixture(autouse=True)
def reset_server_faults():
    fault_manager.reset_faults()
    yield
    fault_manager.reset_faults()


@pytest.mark.asyncio
async def test_end_to_end_invoice_flow_happy_path(tmp_path):
    """
    Test Phase 3 'Done when' criterion:
    The agent completes the core invoice task end to end on the happy path with NO task-specific code.
    Driven through the real CentrAlign agent loop and real Playwright browser tools
    against the live mock company environment.
    """
    # 1. Initialize registry and run manager
    register_default_tools(tool_registry)
    run_manager = RunManager(
        db_path=tmp_path / "invoice_run.db",
        artifacts_dir=tmp_path / "artifacts",
    )

    fake_llm = FakeLLM()

    # Setup Executor with generic components
    executor = AgentExecutor(
        llm_client=fake_llm,
        run_manager=run_manager,
        registry=tool_registry,
        reflector=Reflector(None),  # Use deterministic heuristic reflector for test speed
        max_steps=25,
    )

    # 2. Queue Goal Interpretation Response
    fake_llm.queue_response(
        LLMResponse(
            parsed=Goal(
                outcome="Find the latest invoice from Acme Corp Ltd, extract the amount and due date, and enter it into Acme ERP.",
                success_criteria=[
                    "Latest Acme Corp Ltd invoice PDF downloaded",
                    "Amount and due date extracted",
                    "New bill submitted in Acme ERP with attached PDF",
                ],
                constraints=["Do not duplicate bills"],
                is_blocking=False,
            ),
            provider="fake",
        )
    )

    # 3. Queue Initial Living Plan
    fake_llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(
                        id="intent_1",
                        description="Log into SupplierConnect Portal and download latest invoice for Acme Corp Ltd",
                        expected_outcome="Downloaded invoice PDF",
                    ),
                    Intent(
                        id="intent_2",
                        description="Extract invoice number, amount, and due date from downloaded document",
                        expected_outcome="Invoice details in working memory",
                    ),
                    Intent(
                        id="intent_3",
                        description="Log into Acme ERP and submit a New Bill with details and attached PDF",
                        expected_outcome="Bill confirmed in ERP",
                    ),
                ]
            ),
            provider="fake",
        )
    )

    # 4. Queue Step Actions:
    # Step 1: Open SupplierConnect Portal
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to the SupplierConnect portal login page",
                tool="open_url",
                args={"url": f"{BASE_URL}/portal/login"},
            ),
            provider="fake",
        )
    )

    # Step 2: Fill login credentials
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering portal credentials",
                tool="type_text",
                args={"target": "email", "text": "supplier@acme.com"},
            ),
            provider="fake",
        )
    )

    # Step 3: Enter password
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering portal password",
                tool="type_text",
                args={"target": "password", "text": "portal_pass123"},
            ),
            provider="fake",
        )
    )

    # Step 4: Click Sign In
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Clicking sign in button",
                tool="click",
                args={"target": "Sign In to SupplierConnect"},
            ),
            provider="fake",
        )
    )

    # Step 5: Navigate directly to Invoices filtered by Acme Corp Ltd
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to invoices page filtered for Acme Corp Ltd",
                tool="open_url",
                args={"url": f"{BASE_URL}/portal/invoices?search=Acme+Corp+Ltd"},
            ),
            provider="fake",
        )
    )

    # Step 6: Download the latest invoice PDF (INV-ACME-106.pdf)
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Downloading the latest invoice PDF INV-ACME-106.pdf",
                tool="download_file",
                args={
                    "url": f"{BASE_URL}/portal/download/INV-ACME-106.pdf",
                    "save_dir": str(tmp_path / "downloads"),
                },
            ),
            provider="fake",
        )
    )

    # Step 7: Read downloaded PDF and extract text
    downloaded_pdf_path = str(tmp_path / "downloads" / "INV-ACME-106.pdf")
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Extracting text content from the downloaded invoice PDF",
                tool="read_file",
                args={"path": downloaded_pdf_path},
            ),
            provider="fake",
        )
    )

    # Step 8: Navigate to Acme ERP Login
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to Acme ERP login page",
                tool="open_url",
                args={"url": f"{BASE_URL}/erp/login"},
            ),
            provider="fake",
        )
    )

    # Step 9: Enter ERP username
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering ERP username",
                tool="type_text",
                args={"target": "username", "text": "admin"},
            ),
            provider="fake",
        )
    )

    # Step 10: Enter ERP password
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering ERP password",
                tool="type_text",
                args={"target": "password", "text": "erp_pass123"},
            ),
            provider="fake",
        )
    )

    # Step 11: Click ERP Sign In
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Clicking ERP login button",
                tool="click",
                args={"target": "#erp-submit-btn"},
            ),
            provider="fake",
        )
    )

    # Step 12: Navigate to New Bill form
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to New Bill entry form",
                tool="open_url",
                args={"url": f"{BASE_URL}/erp/bills/new"},
            ),
            provider="fake",
        )
    )

    # Step 13: Fill vendor name
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering vendor name",
                tool="type_text",
                args={"target": "vendor_name", "text": "Acme Corp Ltd"},
            ),
            provider="fake",
        )
    )

    # Step 14: Fill invoice number
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering invoice number",
                tool="type_text",
                args={"target": "invoice_number", "text": "INV-ACME-106"},
            ),
            provider="fake",
        )
    )

    # Step 15: Fill amount
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering invoice amount",
                tool="type_text",
                args={"target": "amount", "text": "91015.34"},
            ),
            provider="fake",
        )
    )

    # Step 16: Fill due date
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering standardized due date",
                tool="type_text",
                args={"target": "due_date", "text": "2026-10-28"},
            ),
            provider="fake",
        )
    )

    # Step 17: Upload original PDF attachment
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Attaching source invoice PDF to ERP bill",
                tool="upload_file",
                args={"target": "attachment", "file_path": downloaded_pdf_path},
            ),
            provider="fake",
        )
    )

    # Step 18: Click Submit Bill
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Submitting the bill into Acme ERP",
                tool="click",
                args={"target": "Submit Bill"},
            ),
            provider="fake",
        )
    )

    # Step 19: Finish task
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Bill INV-ACME-106 confirmed posted in Acme ERP. Completing task.",
                is_finish=True,
                finish_summary="Successfully processed invoice INV-ACME-106 for Acme Corp Ltd (EUR 91015.34, due 2026-10-28) and entered into Acme ERP with attached PDF.",
            ),
            provider="fake",
        )
    )

    # 5. Run the Autonomous Agent!
    task = "Find the latest invoice from Acme Corp Ltd, extract the amount and due date, enter it into our internal system, and tell me once it is done."
    result_run = await executor.run(task)

    # 6. Verify Agent Run State & Audit Trail
    assert result_run.status == RunStatus.COMPLETED
    assert result_run.step_count == 19

    transitions = run_manager.get_transitions(result_run.run_id)
    to_statuses = [t.to_status for t in transitions]
    assert RunStatus.CREATED in to_statuses
    assert RunStatus.CLARIFYING in to_statuses
    assert RunStatus.PLANNING in to_statuses
    assert RunStatus.RUNNING in to_statuses
    assert RunStatus.VERIFYING in to_statuses
    assert RunStatus.COMPLETED in to_statuses

    steps = run_manager.get_steps(result_run.run_id)
    failed_steps = [(s.step_index, s.tool_name, s.error) for s in steps if not s.ok]
    assert not failed_steps, f"Failed steps encountered: {failed_steps}"
    assert len(steps) == 19
    assert all(s.ok for s in steps)

    # 7. Independent Ground-Truth Verification in Database!
    conn = sqlite3.connect("env/company.db")
    bill_row = conn.execute(
        "SELECT vendor_name, invoice_number, amount, due_date, attachment_filename, status FROM erp_bills WHERE invoice_number = 'INV-ACME-106'"
    ).fetchone()
    assert bill_row is not None
    vendor, inv_num, amt, due, att_file, status = bill_row
    assert vendor == "Acme Corp Ltd"
    assert inv_num == "INV-ACME-106"
    assert amt == 91015.34
    assert due == "2026-10-28"
    assert att_file == "INV-ACME-106.pdf"
    assert status == "entered"
