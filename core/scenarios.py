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


def create_renamed_elements_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Happy Path under Renamed Elements / UI Drift: Uses distilled semantic labels resilient to attribute drift."""
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

    # Step 1: Open SupplierConnect Portal
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to portal login", tool="browser_open_url", args={"url": f"{base_url}/portal/login"})))
    # Step 2: Fill email
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering portal credentials", tool="browser_type_text", args={"target": "email", "text": "supplier@acme.com"})))
    # Step 3: Fill password
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering portal password", tool="browser_type_text", args={"target": "password", "text": "portal_pass123"})))
    # Step 4: Click login button
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting portal login form", tool="browser_click", args={"target": "Sign In to SupplierConnect"})))
    # Step 5: Filter invoices by vendor
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to invoices page", tool="browser_open_url", args={"url": f"{base_url}/portal/invoices?search=Acme+Corp+Ltd"})))
    # Step 6: Download PDF
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Downloading latest invoice PDF", tool="browser_download_file", args={"url": f"{base_url}/portal/download/INV-ACME-106.pdf", "save_dir": "artifacts/downloads"})))
    # Step 7: Read downloaded PDF
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Extracting details from PDF", tool="read_file", args={"file_path": "artifacts/downloads/INV-ACME-106.pdf"})))
    # Step 8: Open ERP Login
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Navigating to ERP login", tool="browser_open_url", args={"url": f"{base_url}/erp/login"})))
    # Step 9: Enter ERP username
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering ERP username", tool="browser_type_text", args={"target": "username", "text": "admin"})))
    # Step 10: Enter ERP password
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering ERP password", tool="browser_type_text", args={"target": "password", "text": "erp_pass123"})))
    # Step 11: Click ERP Sign In
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Signing into Acme ERP", tool="browser_click", args={"target": "Sign In to Acme ERP"})))
    # Step 12: Open New Bill form
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Opening New Bill entry form", tool="browser_open_url", args={"url": f"{base_url}/erp/bills/new"})))
    # Step 13: Enter Vendor Name via drifted semantic label
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering vendor name into drifted input", tool="browser_type_text", args={"target": "Supplier Entity Name", "text": "Acme Corp Ltd"})))
    # Step 14: Enter Invoice Number via drifted label
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering invoice number into drifted input", tool="browser_type_text", args={"target": "Bill Reference Number", "text": "INV-ACME-106"})))
    # Step 15: Enter Amount via drifted label
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering amount into drifted input", tool="browser_type_text", args={"target": "Total Amount Payable", "text": "91015.34"})))
    # Step 16: Enter Due Date via drifted label
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Entering due date into drifted input", tool="browser_type_text", args={"target": "Payment Due Date", "text": "2026-10-28"})))
    # Step 17: Upload Attachment via drifted input
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Attaching PDF to bill record", tool="browser_upload_file", args={"target": "bill_document", "file_path": "artifacts/downloads/INV-ACME-106.pdf"})))
    # Step 18: Submit Bill via renamed button
    llm.queue_response(LLMResponse(parsed=AgentAction(thought="Submitting bill via Process & Post Bill button", tool="browser_click", args={"target": "Process & Post Bill"})))
    # Step 19: Finish and trigger independent verifier
    llm.queue_response(
        LLMResponse(
            parsed=AgentAction(
                thought="Bill submitted successfully to Acme ERP despite UI drift. Concluding task for verification.",
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


def create_generalization_task_1_llm(base_url: str = "http://127.0.0.1:8001") -> FakeLLM:
    """Generalization Task 1: Close all tickets assigned to me that were resolved last week and email summary to manager."""
    llm = FakeLLM()

    # 1. Goal
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

    # 2. Plan
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

    # Steps
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

    # 1. Goal
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

    # 2. Plan
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

    # Steps
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

    # 1. Goal
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

    # 2. Plan
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

    # Steps
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


def get_scenario_llm(scenario: int | str, base_url: str = "http://127.0.0.1:8001") -> tuple[FakeLLM, str, dict[str, Any]]:
    """Returns (llm, task, fault_config) for a given scenario number or name."""
    sc_map = {
        "1": 1, "happy": 1,
        "2": 2, "fault": 2, "session": 2,
        "3": 3, "clarify": 3,
        "4": 4, "duplicate": 4,
        "5": 5, "g1": 5, "tickets": 5,
        "6": 6, "g2": 6, "recon": 6, "reconciliation": 6,
        "7": 7, "g3": 7, "onboard": 7, "onboarding": 7,
    }
    sc_id = sc_map.get(str(scenario).lower(), 1)

    if sc_id == 1:
        return create_scenario_1_llm(base_url), SCENARIO_DESCRIPTIONS[1]["task"], {}
    elif sc_id == 2:
        return create_scenario_2_llm(base_url), SCENARIO_DESCRIPTIONS[2]["task"], {"session_expiry_mid_task": True, "session_expiry_target": "erp"}
    elif sc_id == 3:
        return create_scenario_3_llm(), SCENARIO_DESCRIPTIONS[3]["task"], {}
    elif sc_id == 4:
        return create_scenario_4_llm(base_url), SCENARIO_DESCRIPTIONS[4]["task"], {"duplicate_invoice_rejection": True}
    elif sc_id == 5:
        return create_generalization_task_1_llm(base_url), "Close all tickets assigned to me that were resolved last week and email the summary to my manager.", {}
    elif sc_id == 6:
        return create_generalization_task_2_llm(base_url), "Reconcile the vendor statement CSV in the file share against bills in the ERP and list mismatches.", {}
    elif sc_id == 7:
        return create_generalization_task_3_llm(base_url), "Onboard new hire Priya: create the HR ticket, add her to the access sheet, and confirm.", {}

    return create_scenario_1_llm(base_url), SCENARIO_DESCRIPTIONS[1]["task"], {}
