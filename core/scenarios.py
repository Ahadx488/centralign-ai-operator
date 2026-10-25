from __future__ import annotations

from typing import Any

from core.agent.models import AgentAction, Goal, Intent
from core.agent.planner import InitialPlanOutput
from core.llm.fake import FakeLLM
from core.llm.models import LLMResponse

SCENARIO_DESCRIPTIONS: dict[int, dict[str, str]] = {
    1: {
        "name": "Happy Path Invoice Flow",
        "description": "Find latest invoice from Acme Corp Ltd, extract details, and post bill to Acme ERP.",
        "task": "Find the latest invoice from Acme Corp Ltd, extract the amount and due date, enter it into our internal system, and tell me once it is done.",
    },
    2: {
        "name": "Fault Recovery (Session Expiry Mid-Task)",
        "description": "Mid-task session expiration during ERP navigation -> auto-detects auth loss, re-authenticates via system vault, and completes cleanly.",
        "task": "Navigate to Acme ERP bills under session expiry fault.",
    },
    3: {
        "name": "Ambiguous Vendor Clarification",
        "description": "Discovers ambiguity ('Acme Corp' matches Ltd vs Inc) -> pauses in WAITING_USER -> human clarifies -> finishes cleanly.",
        "task": "Find latest invoice from Acme Corp",
    },
    4: {
        "name": "Duplicate Invoice Detection & Prevention",
        "description": "Attempting to re-enter INV-ACME-101 -> detects ERP 409 rejection -> prevents duplicate -> verifies exactly 1 bill exists.",
        "task": "Process invoice INV-ACME-101 for Acme Corp Ltd",
    },
}


def create_scenario_1_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Happy Path: End-to-end invoice download, extraction, ERP entry, and verification."""
    llm = FakeLLM()

    # 1. Goal
    llm.queue_response(
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

    # 2. Plan
    llm.queue_response(
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

    # 3. Actions
    # Step 1: Open SupplierConnect Portal
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to the SupplierConnect portal login page",
                tool="browser_open_url",
                args={"url": f"{base_url}/portal/login"},
            ),
            provider="fake",
        )
    )
    # Step 2: Fill email
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering portal credentials",
                tool="browser_type_text",
                args={"target": "email", "text": "supplier@acme.com"},
            ),
            provider="fake",
        )
    )
    # Step 3: Fill password
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering portal password",
                tool="browser_type_text",
                args={"target": "password", "text": "portal_pass123"},
            ),
            provider="fake",
        )
    )
    # Step 4: Click login button
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Submitting portal login form",
                tool="browser_click",
                args={"target": "Sign In to SupplierConnect"},
            ),
            provider="fake",
        )
    )
    # Step 5: Filter invoices by vendor
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to invoices page filtered for Acme Corp Ltd",
                tool="browser_open_url",
                args={"url": f"{base_url}/portal/invoices?search=Acme+Corp+Ltd"},
            ),
            provider="fake",
        )
    )
    # Step 6: Download PDF
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Downloading the latest invoice PDF INV-ACME-106.pdf",
                tool="browser_download_file",
                args={
                    "url": f"{base_url}/portal/download/INV-ACME-106.pdf",
                    "save_dir": "artifacts/downloads",
                },
            ),
            provider="fake",
        )
    )
    # Step 7: Read downloaded PDF
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Extracting text from downloaded invoice document to determine bill fields",
                tool="read_file",
                args={"file_path": "artifacts/downloads/INV-ACME-106.pdf"},
            ),
            provider="fake",
        )
    )
    # Step 8: Open ERP Login
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Navigating to Acme ERP login page",
                tool="browser_open_url",
                args={"url": f"{base_url}/erp/login"},
            ),
            provider="fake",
        )
    )
    # Step 9: Enter ERP username
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering ERP administrative username",
                tool="browser_type_text",
                args={"target": "username", "text": "admin"},
            ),
            provider="fake",
        )
    )
    # Step 10: Enter ERP password
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering ERP administrative password",
                tool="browser_type_text",
                args={"target": "password", "text": "erp_pass123"},
            ),
            provider="fake",
        )
    )
    # Step 11: Click ERP Sign In
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Signing into Acme ERP",
                tool="browser_click",
                args={"target": "Sign In to Acme ERP"},
            ),
            provider="fake",
        )
    )
    # Step 12: Open New Bill form
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Opening the New Bill entry form in Acme ERP",
                tool="browser_open_url",
                args={"url": f"{base_url}/erp/bills/new"},
            ),
            provider="fake",
        )
    )
    # Step 13: Enter Vendor Name
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering vendor name into bill register",
                tool="browser_type_text",
                args={"target": "vendor_name", "text": "Acme Corp Ltd"},
            ),
            provider="fake",
        )
    )
    # Step 14: Enter Invoice Number
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering invoice number into bill register",
                tool="browser_type_text",
                args={"target": "invoice_number", "text": "INV-ACME-106"},
            ),
            provider="fake",
        )
    )
    # Step 15: Enter Amount
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering total invoice amount into bill register",
                tool="browser_type_text",
                args={"target": "amount", "text": "91015.34"},
            ),
            provider="fake",
        )
    )
    # Step 16: Enter Due Date
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Entering due date into bill register",
                tool="browser_type_text",
                args={"target": "due_date", "text": "2026-10-28"},
            ),
            provider="fake",
        )
    )
    # Step 17: Upload Attachment
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Attaching invoice PDF to bill record",
                tool="browser_upload_file",
                args={
                    "target": "attachment",
                    "file_path": "artifacts/downloads/INV-ACME-106.pdf",
                },
            ),
            provider="fake",
        )
    )
    # Step 18: Submit Bill
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Submitting completed bill to Acme ERP register",
                tool="browser_click",
                args={"target": "Submit Bill"},
            ),
            provider="fake",
        )
    )
    # Step 19: Finish and trigger independent verifier
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Bill submitted successfully to Acme ERP. Concluding task for verification.",
                is_finish=True,
                finish_summary="Successfully processed invoice INV-ACME-106 from Acme Corp Ltd for EUR 91,015.34 into Acme ERP.",
            ),
            provider="fake",
        )
    )

    return llm


def create_scenario_2_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Scenario 2: Mid-task session expiry recovery."""
    llm = FakeLLM()

    llm.queue_response(
        LLMResponse(
            parsed=Goal(outcome="Access ERP bills under session expiry fault", is_blocking=False),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(id="i1", description="Access ERP bills register", expected_outcome="Bills list accessed")
                ]
            ),
            provider="fake",
        )
    )

    # 1. Login to ERP
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Open login", tool="browser_open_url", args={"url": f"{base_url}/erp/login"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Enter user", tool="browser_type_text", args={"target": "#username", "text": "admin"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Enter pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Sign in", tool="browser_click", args={"target": "#erp-submit-btn"}),
            provider="fake",
        )
    )

    # 2. Access bills -> redirects to /erp/login?expired=1
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Access bills", tool="browser_open_url", args={"url": f"{base_url}/erp/bills"}),
            provider="fake",
        )
    )

    # 3. Agent re-authenticates and navigates back
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Re-login to ERP", tool="browser_type_text", args={"target": "#username", "text": "admin"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Re-enter pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Sign in again", tool="browser_click", args={"target": "#erp-submit-btn"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Re-access bills", tool="browser_open_url", args={"url": f"{base_url}/erp/bills"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Session successfully restored, finishing",
                is_finish=True,
                finish_summary="Recovered from expired session and successfully accessed ERP bills.",
            ),
            provider="fake",
        )
    )

    return llm


def create_scenario_3_llm() -> FakeLLM:
    """Scenario 3: Ambiguous vendor clarification."""
    llm = FakeLLM()

    llm.queue_response(
        LLMResponse(
            parsed=Goal(outcome="Process Acme Corp invoice", is_blocking=False),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(id="i1", description="Clarify ambiguous vendor", expected_outcome="Vendor clarified")
                ]
            ),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Discovered multiple vendors matching 'Acme Corp': Acme Corp Ltd and Acme Corp Inc. Asking human for clarification per policy.",
                tool="ask_user",
                args={
                    "question": "Which vendor do you want to process: Acme Corp Ltd or Acme Corp Inc?",
                    "options": ["Acme Corp Ltd", "Acme Corp Inc"],
                },
            ),
            provider="fake",
        )
    )
    # Next response when resumed with clarification
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Received user clarification: Acme Corp Ltd confirmed. Proceeding to finish.",
                is_finish=True,
                finish_summary="Target vendor confirmed as Acme Corp Ltd per human clarification.",
            ),
            provider="fake",
        )
    )

    return llm


def create_scenario_4_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Scenario 4: Duplicate invoice rejection and reporting."""
    llm = FakeLLM()

    llm.queue_response(
        LLMResponse(
            parsed=Goal(outcome="Enter invoice INV-ACME-101", is_blocking=False),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=InitialPlanOutput(
                intents=[
                    Intent(id="i1", description="Post bill to ERP", expected_outcome="Bill entered or checked")
                ]
            ),
            provider="fake",
        )
    )

    # Login to ERP
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Login ERP", tool="browser_open_url", args={"url": f"{base_url}/erp/login"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="User", tool="browser_type_text", args={"target": "#username", "text": "admin"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Pass", tool="browser_type_text", args={"target": "#password", "text": "erp_pass123"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Sign in", tool="browser_click", args={"target": "#erp-submit-btn"}),
            provider="fake",
        )
    )

    # Submit duplicate bill
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Go to new bill", tool="browser_open_url", args={"url": f"{base_url}/erp/bills/new"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Vendor", tool="browser_type_text", args={"target": "vendor_name", "text": "Acme Corp Ltd"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Invoice Num", tool="browser_type_text", args={"target": "invoice_number", "text": "INV-ACME-101"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Amount", tool="browser_type_text", args={"target": "amount", "text": "34500.00"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Due Date", tool="browser_type_text", args={"target": "due_date", "text": "2026-10-25"}),
            provider="fake",
        )
    )
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(thought="Submit", tool="browser_click", args={"target": "Submit Bill"}),
            provider="fake",
        )
    )

    # Duplicate detected banner seen
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Duplicate invoice detected: Bill for vendor Acme Corp Ltd and invoice INV-ACME-101 already entered in ERP. Reporting without double-entering.",
                is_finish=True,
                finish_summary="Invoice INV-ACME-101 already exists in Acme ERP. Skipped duplicate entry per non-negotiable reliability rule.",
            ),
            provider="fake",
        )
    )

    return llm


def get_scenario_llm(scenario: int | str, base_url: str = "http://127.0.0.1:8001") -> tuple[FakeLLM, str, dict[str, Any]]:
    """Returns (llm, task, fault_config) for a given scenario number or name."""
    sc_map = {"1": 1, "happy": 1, "2": 2, "fault": 2, "session": 2, "3": 3, "clarify": 3, "4": 4, "duplicate": 4}
    sc_id = sc_map.get(str(scenario).lower(), 1)

    info = SCENARIO_DESCRIPTIONS.get(sc_id, SCENARIO_DESCRIPTIONS[1])
    task = info["task"]

    if sc_id == 1:
        return create_scenario_1_llm(base_url), task, {}
    elif sc_id == 2:
        return create_scenario_2_llm(base_url), task, {"session_expiry_mid_task": True, "session_expiry_target": "erp"}
    elif sc_id == 3:
        return create_scenario_3_llm(), task, {}
    elif sc_id == 4:
        return create_scenario_4_llm(base_url), task, {"duplicate_invoice_rejection": True}

    return create_scenario_1_llm(base_url), task, {}
