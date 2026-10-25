from __future__ import annotations

import io
import time

from fastapi.testclient import TestClient
from pypdf import PdfReader

from env.faults.manager import fault_manager
from env.seed.generator import seed_company_data
from env.server import app

client = TestClient(app)


def setup_function():
    """Ensure clean seed state before each test."""
    seed_company_data()
    fault_manager.reset_faults()


# ---------------------------------------------------------------------------
# 1. Authentication Tests
# ---------------------------------------------------------------------------


def test_erp_login_flow():
    """Verify ERP authentication, cookie issuance, and unauthorized redirection."""
    # 1. Unauthenticated access redirects to login
    unauth_res = client.get("/erp/bills", follow_redirects=False)
    assert unauth_res.status_code == 303
    assert "/erp/login" in unauth_res.headers["location"]

    # 2. Invalid credentials returns 401
    bad_login = client.post(
        "/erp/login",
        data={"username": "admin", "password": "wrong_password"},
    )
    assert bad_login.status_code == 401
    assert "Invalid ERP username or password" in bad_login.text

    # 3. Successful login sets session_erp cookie
    good_login = client.post(
        "/erp/login",
        data={"username": "admin", "password": "erp_pass123"},
        follow_redirects=False,
    )
    assert good_login.status_code == 303
    assert "session_erp" in good_login.cookies

    # 4. Authenticated request succeeds
    auth_client = TestClient(app, cookies={"session_erp": "admin"})
    bills_page = auth_client.get("/erp/bills")
    assert bills_page.status_code == 200
    assert "Accounts Payable - Bills Register" in bills_page.text


def test_portal_login_flow():
    """Verify SupplierConnect Portal authentication and independent session."""
    unauth = client.get("/portal/invoices", follow_redirects=False)
    assert unauth.status_code == 303
    assert "/portal/login" in unauth.headers["location"]

    good_login = client.post(
        "/portal/login",
        data={"email": "supplier@acme.com", "password": "portal_pass123"},
        follow_redirects=False,
    )
    assert good_login.status_code == 303
    assert "session_portal" in good_login.cookies

    auth_client = TestClient(app, cookies={"session_portal": "supplier@acme.com"})
    invoices_page = auth_client.get("/portal/invoices")
    assert invoices_page.status_code == 200
    assert "Published Invoices" in invoices_page.text


def test_ticketing_login_flow():
    """Verify Service Desk login and independent session."""
    good_login = client.post(
        "/tickets/login",
        data={"username": "operator", "password": "tickets_pass123"},
        follow_redirects=False,
    )
    assert good_login.status_code == 303
    assert "session_tickets" in good_login.cookies


# ---------------------------------------------------------------------------
# 2. Happy Path Invoice Flow & PDF Text Extractability
# ---------------------------------------------------------------------------


def test_happy_path_invoice_workflow():
    """
    Simulate the complete hand-off:
    1. Search & find invoice on Vendor Portal
    2. Download PDF
    3. Extract text cleanly with pypdf
    4. Enter Bill into Acme ERP with attachment
    5. Verify entry in ERP and Ground Truth API
    """
    portal_client = TestClient(app, cookies={"session_portal": "supplier@acme.com"})
    erp_client = TestClient(app, cookies={"session_erp": "admin"})

    # 1. Search for Acme Corp Ltd invoice
    res = portal_client.get("/portal/invoices?q=INV-ACME-102")
    assert res.status_code == 200
    assert "INV-ACME-102" in res.text
    assert "Acme Corp Ltd" in res.text

    # 2. Download PDF
    pdf_res = portal_client.get("/portal/download/INV-ACME-102.pdf")
    assert pdf_res.status_code == 200
    assert pdf_res.headers["content-type"] == "application/pdf"
    pdf_bytes = pdf_res.content

    # 3. Extract text using pypdf (verifies digital text generation without OCR)
    reader = PdfReader(io.BytesIO(pdf_bytes))
    extracted_text = reader.pages[0].extract_text()
    assert "Acme Corp Ltd" in extracted_text
    assert "INV-ACME-102" in extracted_text
    assert "TAX INVOICE" in extracted_text

    # 4. Enter Bill into Acme ERP
    bill_data = {
        "vendor_name": "Acme Corp Ltd",
        "invoice_number": "INV-ACME-102",
        "amount": "12500.00",
        "currency": "INR",
        "due_date": "2026-11-15",
    }
    files = {"attachment": ("INV-ACME-102.pdf", pdf_bytes, "application/pdf")}

    submit_res = erp_client.post(
        "/erp/bills/new", data=bill_data, files=files, follow_redirects=True
    )
    assert submit_res.status_code == 200
    assert "Successfully entered bill for invoice INV-ACME-102" in submit_res.text

    # 5. Verify via Ground Truth API
    truth_bills = client.get("/__truth/bills").json()
    matching = [b for b in truth_bills if b["invoice_number"] == "INV-ACME-102"]
    assert len(matching) == 1
    assert matching[0]["vendor_name"] == "Acme Corp Ltd"
    assert matching[0]["amount"] == 12500.00


# ---------------------------------------------------------------------------
# 3. Duplicate Bill Rejection
# ---------------------------------------------------------------------------


def test_duplicate_bill_rejection():
    """Verify that re-entering an existing bill triggers duplicate rejection."""
    erp_client = TestClient(app, cookies={"session_erp": "admin"})

    # Attempt to submit already seeded INV-ACME-101 for Acme Corp Ltd
    dup_data = {
        "vendor_name": "Acme Corp Ltd",
        "invoice_number": "INV-ACME-101",
        "amount": "34500.00",
        "currency": "INR",
        "due_date": "2026-10-25",
    }
    res = erp_client.post("/erp/bills/new", data=dup_data)
    assert res.status_code == 409
    assert "Duplicate bill rejected" in res.text
    assert "INV-ACME-101" in res.text


# ---------------------------------------------------------------------------
# 4. Dynamic Fault Injection Matrix
# ---------------------------------------------------------------------------


def test_fault_slow_page():
    """Verify slow page delay fault."""
    fault_manager.update_faults({"slow_page": True, "slow_page_delay_sec": 0.2})
    portal_client = TestClient(app, cookies={"session_portal": "supplier@acme.com"})

    start = time.time()
    res = portal_client.get("/portal/invoices")
    elapsed = time.time() - start

    assert res.status_code == 200
    assert elapsed >= 0.18


def test_fault_renamed_elements():
    """Verify UI drift fault changes form element names and labels, but submission works with drifted names."""
    erp_client = TestClient(app, cookies={"session_erp": "admin"})

    # Canonical form inspection
    canonical_form = erp_client.get("/erp/bills/new")
    assert 'name="vendor_name"' in canonical_form.text
    assert 'id="submit-bill-btn"' in canonical_form.text

    # Activate UI drift fault
    client.post("/__truth/faults", json={"renamed_elements": True})

    drifted_form = erp_client.get("/erp/bills/new")
    assert 'name="supplier_entity"' in drifted_form.text
    assert 'name="total_payable"' in drifted_form.text
    assert 'id="process-post-bill-btn"' in drifted_form.text
    assert 'name="vendor_name"' not in drifted_form.text

    # Submitting with drifted names works
    drifted_data = {
        "supplier_entity": "Stark Logistics Pvt Ltd",
        "bill_reference_no": "INV-STARK-DRIFT-01",
        "total_payable": "54000.00",
        "currency_code": "INR",
        "payment_due_date": "2026-11-20",
    }
    submit_res = erp_client.post("/erp/bills/new", data=drifted_data, follow_redirects=True)
    assert submit_res.status_code == 200
    assert "INV-STARK-DRIFT-01" in submit_res.text


def test_fault_session_expiry_mid_task():
    """Verify mid-task session expiry forces re-login."""
    erp_client = TestClient(app, cookies={"session_erp": "admin"})

    # Pre-condition: authenticated
    res1 = erp_client.get("/erp/bills", follow_redirects=False)
    assert res1.status_code == 200

    # Inject expiry fault
    client.post(
        "/__truth/faults", json={"session_expiry_mid_task": True, "session_expiry_target": "erp"}
    )

    # Next request triggers expiry redirect
    res2 = erp_client.get("/erp/bills", follow_redirects=False)
    assert res2.status_code == 303
    assert "/erp/login?expired=1" in res2.headers["location"]


def test_fault_transient_500():
    """Verify transient 500 error on first attempt, auto-recovery on retry."""
    client.post("/__truth/faults", json={"transient_500": True, "transient_500_count": 1})
    portal_client = TestClient(app, cookies={"session_portal": "supplier@acme.com"})

    # Attempt 1: 500
    res1 = portal_client.get("/portal/invoices")
    assert res1.status_code == 500

    # Attempt 2: Auto-recovered (count decremented to 0)
    res2 = portal_client.get("/portal/invoices")
    assert res2.status_code == 200


def test_fault_missing_portal_invoice():
    """Verify invoice is hidden from portal when missing fault is active."""
    portal_client = TestClient(app, cookies={"session_portal": "supplier@acme.com"})

    # By default, INV-GLOBEX-901 was seeded with is_on_portal=0
    res = portal_client.get("/portal/invoices?q=INV-GLOBEX-901")
    # Verify the invoice row is not present in the results table
    assert 'data-invoice-number="INV-GLOBEX-901"' not in res.text
    assert "No invoices matching query" in res.text

    # Verify it is present in Mailbox
    mail_res = client.get("/mail/inbox?q=INV-GLOBEX-901")
    assert mail_res.status_code == 200
    assert "INV-GLOBEX-901" in mail_res.text


# ---------------------------------------------------------------------------
# 5. Alternative Sources (Mailbox & Fileshare)
# ---------------------------------------------------------------------------


def test_mailbox_and_fileshare_browsing():
    """Verify emails and shared files can be inspected and downloaded."""
    # Mailbox
    inbox = client.get("/mail/inbox")
    assert inbox.status_code == 200
    assert "Corporate Mailbox" in inbox.text

    # View email with attachment
    emails = client.get("/__truth/emails").json()
    assert len(emails) > 0
    attached_emails = [e for e in emails if e.get("attachment_filename") == "INV-GLOBEX-901.pdf"]
    assert len(attached_emails) > 0
    email_view = client.get(f"/mail/view/{attached_emails[0]['id']}")
    assert email_view.status_code == 200
    assert "INV-GLOBEX-901.pdf" in email_view.text

    # Fileshare browse
    files = client.get("/files/browse")
    assert files.status_code == 200
    assert "AcmeCorp_Oct2026_statement.csv" in files.text
    assert "INV_Final_v2_Scanned_042.csv" in files.text

    # Download CSV
    csv_res = client.get("/files/download/AcmeCorp_Oct2026_statement.csv")
    assert csv_res.status_code == 200
    assert "Acme Corp Ltd" in csv_res.text


# ---------------------------------------------------------------------------
# 6. Ticketing & HR Portal
# ---------------------------------------------------------------------------


def test_ticketing_crud_operations():
    """Verify ticket list, creation, and status updates."""
    tickets_client = TestClient(app, cookies={"session_tickets": "operator"})

    # 1. List
    list_res = tickets_client.get("/tickets/list")
    assert list_res.status_code == 200
    assert "TICK-101" in list_res.text

    # 2. Create new ticket
    new_ticket_data = {
        "title": "Onboard new hire Priya Patel",
        "description": "Issue corporate laptop and grant ERP read access.",
        "assignee": "Deepak Sharma",
        "priority": "high",
    }
    create_res = tickets_client.post("/tickets/new", data=new_ticket_data, follow_redirects=True)
    assert create_res.status_code == 200
    assert "Onboard new hire Priya Patel" in create_res.text

    # 3. Update ticket status
    update_data = {"status": "resolved", "assignee": "Deepak Sharma"}
    update_res = tickets_client.post(
        "/tickets/TICK-101/update", data=update_data, follow_redirects=True
    )
    assert update_res.status_code == 200
    assert "resolved" in update_res.text


# ---------------------------------------------------------------------------
# 7. Ground Truth API
# ---------------------------------------------------------------------------


def test_ground_truth_state_and_reset():
    """Verify ground-truth endpoints expose unadulterated state and reset works."""
    state = client.get("/__truth/state").json()
    assert "erp_bills" in state
    assert "invoices" in state
    assert "vendors" in state
    assert "emails" in state
    assert "tickets" in state
    assert "faults" in state
    assert len(state["vendors"]) == 8

    # Reset environment
    reset_res = client.post("/__truth/reset").json()
    assert reset_res["status"] == "reset_success"
