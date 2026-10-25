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
from core.verify.models import VerificationOutcome
from core.verify.verifier import IndependentVerifier
from env.faults.manager import fault_manager
from tests.conftest import BASE_URL


@pytest.fixture(autouse=True)
def setup_tools_and_env():
    register_default_tools(tool_registry)
    fault_manager.reset_faults()
    yield
    fault_manager.reset_faults()


# ---------------------------------------------------------------------------
# 1. Independent Verifier: Catch Deliberately Wrong Amount & Prevent False Completion
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_verifier_catches_wrong_amount_and_prevents_false_completion(tmp_path):
    """
    Test Hard Rule #4: The Independent Verifier MUST NOT trust the agent's claim.
    When an LLM error or OCR noise causes the agent to enter an incorrect amount (e.g. 50000.00 instead of 64977.03),
    the verifier detects the discrepancy against the source document on disk, returns FAIL, and blocks false completion.
    """
    run_manager = RunManager(
        db_path=tmp_path / "verifier_fail_run.db",
        artifacts_dir=tmp_path / "artifacts",
    )
    verifier = IndependentVerifier(
        db_path="env/company.db",
        artifacts_dir=tmp_path / "artifacts",
    )

    # 1. Simulate a run where a bill was inserted with wrong amount
    conn = sqlite3.connect("env/company.db")
    with conn:
        conn.execute("DELETE FROM erp_bills WHERE invoice_number = 'INV-ACME-102'")
        conn.execute(
            """
            INSERT INTO erp_bills (vendor_name, invoice_number, amount, currency, due_date, attachment_filename, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            ("Acme Corp Ltd", "INV-ACME-102", 50000.00, "INR", "2026-07-28", "INV-ACME-102.pdf", "entered"),
        )

    run = run_manager.create_run(task="Process invoice INV-ACME-102 for Acme Corp Ltd")
    goal = Goal(
        outcome="Process invoice INV-ACME-102 for Acme Corp Ltd",
        success_criteria=["Bill created with matching amount"],
    )
    working_memory = {
        "vendor_name": "Acme Corp Ltd",
        "invoice_number": "INV-ACME-102",
        "amount": 50000.00,  # Agent claims 50,000 entered
    }

    # 2. Run Independent Verification against source PDF on disk
    # The source PDF INV-ACME-102 has amount 64977.03!
    verif_res = await verifier.verify(
        run=run,
        goal=goal,
        working_memory=working_memory,
        steps=[],
    )

    # 3. Assert Verifier catches discrepancy and rejects completion!
    assert verif_res.outcome == VerificationOutcome.FAIL
    assert any("Amount discrepancy" in d for d in verif_res.discrepancies)
    assert any(c.check_name == "amount_cross_check" and not c.passed for c in verif_res.checks)

    # Check evidence bundle markdown output
    md_report = verif_res.evidence_bundle.to_markdown()
    assert "❌ FAIL" in md_report
    assert "INV-ACME-102" in md_report
    assert "Amount discrepancy" in md_report


# ---------------------------------------------------------------------------
# 2. Fault: Slow Page / Timeout
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_slow_page_resilience(tmp_path):
    """
    Verify agent handles slow pages with settle detection without spamming clicks.
    """
    fault_manager.update_faults({"slow_page": True, "slow_page_delay_sec": 1.0})

    run_manager = RunManager(db_path=tmp_path / "slow_page.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Check portal", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Open portal", expected_outcome="Page loaded")]), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Opening portal under slow network", tool="browser_open_url", args={"url": f"{BASE_URL}/portal/login"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Portal loaded, finishing", is_finish=True, finish_summary="Portal loaded despite delay"), provider="fake"))

    run = await executor.run("Check portal under slow page fault")
    assert run.status == RunStatus.COMPLETED
    assert run.step_count == 2


# ---------------------------------------------------------------------------
# 3. Fault: Renamed Elements (UI Drift)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_renamed_elements_ui_drift_recovery(tmp_path):
    """
    Verify agent interacts with drifted/renamed UI elements via DOM snapshot semantic refs.
    """
    fault_manager.update_faults({"renamed_elements": True})

    run_manager = RunManager(db_path=tmp_path / "ui_drift.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="ERP bill form", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Fill drifted form", expected_outcome="Form filled")]), provider="fake"))

    # Login to ERP
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Login ERP", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/login"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Enter user", tool="browser_type_text", args={"target": "#username", "text": "admin"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Enter pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submit login", tool="browser_click", args={"target": "#erp-submit-btn"}), provider="fake"))

    # Open drifted New Bill form
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Go to new bill", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/bills/new"}), provider="fake"))

    # Snapshot to resolve new refs
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Take snapshot to inspect drifted elements", tool="browser_snapshot", args={}), provider="fake"))

    # Type into renamed element 'Supplier Entity Name' using semantic label match
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Type supplier entity name", tool="browser_type_text", args={"target": "Supplier Entity Name", "text": "Acme Corp Ltd"}), provider="fake"))

    # Type into renamed bill reference no
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Type bill reference no", tool="browser_type_text", args={"target": "Bill Reference Number", "text": "INV-DRIFT-100"}), provider="fake"))

    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Done testing drift", is_finish=True, finish_summary="Successfully located drifted form inputs"), provider="fake"))

    run = await executor.run("Fill drifted ERP form")
    assert run.status == RunStatus.COMPLETED
    assert run.step_count == 9


# ---------------------------------------------------------------------------
# 4. Fault: Session Expiry Mid-Task Auto-Recovery
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_session_expiry_mid_task_recovery(tmp_path):
    """
    Verify agent detects mid-task session expiration (?expired=1) and auto-reauthenticates.
    """
    run_manager = RunManager(db_path=tmp_path / "session_expiry.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Session recovery", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Access ERP bills", expected_outcome="Bills list accessed")]), provider="fake"))

    # 1. Login to ERP first
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Open login", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/login"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Enter user", tool="browser_type_text", args={"target": "#username", "text": "admin"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Enter pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Sign in", tool="browser_click", args={"target": "#erp-submit-btn"}), provider="fake"))

    # 2. Trigger session expiry fault via API
    fault_manager.update_faults({"session_expiry_mid_task": True, "session_expiry_target": "erp"})

    # 3. Next navigation gets redirected to /erp/login?expired=1
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Access bills", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/bills"}), provider="fake"))

    # 4. Agent sees expired session, re-logs in and finishes
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Re-login to ERP", tool="browser_type_text", args={"target": "#username", "text": "admin"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Re-enter pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Sign in again", tool="browser_click", args={"target": "#erp-submit-btn"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Re-access bills", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/bills"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Session successfully restored, finishing", is_finish=True, finish_summary="Recovered from expired session and accessed bills"), provider="fake"))

    run = await executor.run("Navigate ERP with session expiry")
    assert run.status == RunStatus.COMPLETED
    assert run.step_count == 10


# ---------------------------------------------------------------------------
# 5. Fault: Transient 500 Backoff Retry
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_transient_500_backoff_retry(tmp_path):
    """
    Verify agent handles transient HTTP 500 error with automatic backoff retry.
    """
    fault_manager.update_faults({"transient_500": True, "transient_500_count": 1})

    run_manager = RunManager(db_path=tmp_path / "transient_500.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Fetch portal invoices", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Call API", expected_outcome="API success")]), provider="fake"))

    # Tool execution will hit 500 on first try, then auto-retry and succeed
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Calling endpoint", tool="http_request", args={"method": "GET", "url": f"{BASE_URL}/portal/invoices"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Endpoint succeeded on retry", is_finish=True, finish_summary="Succeeded after transient 500 recovery"), provider="fake"))

    run = await executor.run("Test transient 500")
    assert run.status == RunStatus.COMPLETED
    assert run.step_count == 2


# ---------------------------------------------------------------------------
# 6. Fault: Missing Portal Invoice (Source-Switching Recovery)
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_missing_portal_invoice_source_switching(tmp_path):
    """
    Verify agent detects invoice missing from portal and switches sources (Portal -> Mailbox/Fileshare).
    INV-GLOBEX-901 is missing from portal, but present in Mailbox and Shared Files!
    """
    fault_manager.update_faults({"missing_portal_invoice": True, "missing_invoice_number": "INV-GLOBEX-901"})

    run_manager = RunManager(db_path=tmp_path / "missing_source.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Find invoice INV-GLOBEX-901", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Check Portal for INV-GLOBEX-901", expected_outcome="Found in portal")]), provider="fake"))

    # Step 1: Login to portal
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Open portal", tool="browser_open_url", args={"url": f"{BASE_URL}/portal/login"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Email", tool="browser_type_text", args={"target": "#email", "text": "supplier@acme.com"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Pass", tool="browser_type_text", args={"target": "#password", "text": "portal_pass123"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Login", tool="browser_click", args={"target": "#portal-submit-btn"}), provider="fake"))

    # Step 2: Search portal for INV-GLOBEX-901 -> yields "No invoices matching query"
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Search for invoice", tool="browser_open_url", args={"url": f"{BASE_URL}/portal/invoices?q=INV-GLOBEX-901"}), provider="fake"))

    # Step 3: Re-plan triggered by Reflector -> Switch source to Mailbox!
    fake_llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(intents=[Intent(id="alt_mailbox", description="Search Corporate Mailbox for INV-GLOBEX-901", expected_outcome="Found in mail")]),
            provider="fake",
        )
    )

    # Step 4: Search Mailbox
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Invoice not in portal! Searching Corporate Mailbox per procedure.",
                tool="mail_search",
                args={"query": "INV-GLOBEX-901"},
            ),
            provider="fake",
        )
    )

    # Step 5: Save attachment from discovered email
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Found email containing invoice! Saving attachment.",
                tool="mail_save_attachment",
                args={"filename": "INV-GLOBEX-901.pdf", "save_dir": str(tmp_path / "downloads")},
            ),
            provider="fake",
        )
    )

    # Step 6: Complete
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Successfully retrieved missing invoice from corporate mailbox",
                is_finish=True,
                finish_summary="Source switched: Located and downloaded INV-GLOBEX-901 from corporate mailbox after portal miss.",
            ),
            provider="fake",
        )
    )

    run = await executor.run("Locate invoice INV-GLOBEX-901")
    assert run.status == RunStatus.COMPLETED
    assert run.step_count == 8

    # Verify that mail attachment was actually downloaded
    downloaded = tmp_path / "downloads" / "INV-GLOBEX-901.pdf"
    assert downloaded.exists()


# ---------------------------------------------------------------------------
# 7. Fault: Duplicate Invoice Rejection in ERP
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_fault_duplicate_invoice_detection_and_reporting(tmp_path):
    """
    Verify agent recognizes duplicate bill rejection (INV-ACME-101),
    avoids duplicate entry, and completes with verification confirmation.
    """
    run_manager = RunManager(db_path=tmp_path / "duplicate_run.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Enter invoice INV-ACME-101", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Post bill to ERP", expected_outcome="Bill entered or checked")]), provider="fake"))

    # Login to ERP
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Login ERP", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/login"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="User", tool="browser_type_text", args={"target": "#username", "text": "admin"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Sign in", tool="browser_click", args={"target": "#erp-submit-btn"}), provider="fake"))

    # Try to submit existing duplicate bill INV-ACME-101
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Go to new bill", tool="browser_open_url", args={"url": f"{BASE_URL}/erp/bills/new"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Vendor", tool="browser_type_text", args={"target": "vendor_name", "text": "Acme Corp Ltd"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Invoice Num", tool="browser_type_text", args={"target": "invoice_number", "text": "INV-ACME-101"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Amount", tool="browser_type_text", args={"target": "amount", "text": "34500.00"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Due Date", tool="browser_type_text", args={"target": "due_date", "text": "2026-10-25"}), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submit", tool="browser_click", args={"target": "Submit Bill"}), provider="fake"))

    # The submission will show duplicate banner! Reflector marks duplicate detected.
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Duplicate invoice detected: Bill for vendor Acme Corp Ltd and invoice INV-ACME-101 already exists. Reporting without double-entering.",
                is_finish=True,
                finish_summary="Invoice INV-ACME-101 already exists in Acme ERP. Skipped duplicate submission.",
            ),
            provider="fake",
        )
    )

    run = await executor.run("Process invoice INV-ACME-101")
    assert run.status == RunStatus.COMPLETED
    assert run.working_memory.get("is_duplicate") is True

    # Verify in DB that only 1 bill exists (no duplicate created!)
    conn = sqlite3.connect("env/company.db")
    rows = conn.execute("SELECT id FROM erp_bills WHERE invoice_number = 'INV-ACME-101'").fetchall()
    assert len(rows) == 1


# ---------------------------------------------------------------------------
# 8. Ambiguous Vendor Name Clarification
# ---------------------------------------------------------------------------
@pytest.mark.asyncio
async def test_ambiguous_vendor_clarification(tmp_path):
    """
    Verify agent asks for clarification when ambiguous vendor match is encountered
    (e.g., 'Acme Corp' matches both 'Acme Corp Ltd' and 'Acme Corp Inc').
    """
    run_manager = RunManager(db_path=tmp_path / "clarify.db", artifacts_dir=tmp_path / "artifacts")
    fake_llm = FakeLLM()
    executor = AgentExecutor(llm_client=fake_llm, run_manager=run_manager, reflector=Reflector(None))

    fake_llm.queue_response(LLMResponse(parsed=Goal(outcome="Process Acme Corp invoice", is_blocking=False), provider="fake"))
    fake_llm.queue_response(LLMResponse(parsed=InitialPlanOutput(intents=[Intent(id="i1", description="Clarify ambiguous vendor", expected_outcome="Vendor clarified")]), provider="fake"))

    # Agent calls ask_user
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Found multiple vendors matching 'Acme Corp': Acme Corp Ltd and Acme Corp Inc. Asking user for clarification.",
                tool="ask_user",
                args={
                    "question": "Which vendor do you want to process: Acme Corp Ltd or Acme Corp Inc?",
                    "options": ["Acme Corp Ltd", "Acme Corp Inc"],
                },
            ),
            provider="fake",
        )
    )

    # 1. Run should pause in WAITING_USER
    paused_run = await executor.run("Find latest invoice from Acme Corp")
    assert paused_run.status == RunStatus.WAITING_USER
    assert paused_run.working_memory["pending_question"] == "Which vendor do you want to process: Acme Corp Ltd or Acme Corp Inc?"

    # 2. User responds with "Acme Corp Ltd"
    fake_llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Received user clarification: Acme Corp Ltd. Proceeding.",
                is_finish=True,
                finish_summary="Task clarified: Target vendor confirmed as Acme Corp Ltd.",
            ),
            provider="fake",
        )
    )

    resumed_run = await executor.resume(paused_run.run_id, human_input="Acme Corp Ltd")
    assert resumed_run.status == RunStatus.COMPLETED
    assert resumed_run.working_memory["user_clarification"] == "Acme Corp Ltd"
