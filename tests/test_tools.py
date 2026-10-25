from __future__ import annotations

import os

import pytest

from core.tools import get_browser_manager, tool_registry
from env.faults.manager import fault_manager
from tests.conftest import BASE_URL

# ---------------------------------------------------------------------------
# 1. Registry & Schemas
# ---------------------------------------------------------------------------


def test_registry_registration_and_export():
    """Verify all required tools are registered with schemas and risk tiers."""
    tools = tool_registry.list_tools()
    tool_names = [t.name for t in tools]

    expected_tools = [
        "browser_open_url",
        "browser_snapshot",
        "browser_click",
        "browser_type_text",
        "browser_select_option",
        "browser_press_key",
        "browser_wait_for",
        "browser_screenshot",
        "browser_extract_text",
        "browser_download_file",
        "browser_upload_file",
        "browser_get_tabs",
        "files_list_dir",
        "files_read_file",
        "files_write_file",
        "files_search_files",
        "http_request",
        "mail_search",
        "mail_read",
        "mail_save_attachment",
        "ask_user",
        "request_approval",
    ]

    for expected in expected_tools:
        assert expected in tool_names, f"Expected tool '{expected}' missing from registry"

    # Export to LLM tool definitions
    definitions = tool_registry.export_definitions()
    assert len(definitions) == len(tools)
    open_url_def = next(d for d in definitions if d.name == "browser_open_url")
    assert "url" in open_url_def.parameters["properties"]


# ---------------------------------------------------------------------------
# 2. File Tools Suite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_file_tools_suite():
    """Verify list_dir, search, read (PDF and CSV), and sandboxed write."""
    # 1. List directory
    list_res = await tool_registry.execute("files_list_dir", {"path": "./env/fileshare/data"})
    assert list_res.ok
    assert any(e["name"] == "invoices" for e in list_res.data["entries"])

    # 2. Search files
    search_res = await tool_registry.execute(
        "files_search_files", {"pattern": "*.pdf", "base_dir": "./env/fileshare/data"}
    )
    assert search_res.ok
    assert len(search_res.data["matches"]) > 0

    # 3. Read PDF text extraction via pypdf
    first_pdf = search_res.data["matches"][0]["full_path"]
    read_pdf_res = await tool_registry.execute("files_read_file", {"path": first_pdf})
    assert read_pdf_res.ok
    assert read_pdf_res.data["format"] == "pdf"
    assert "TAX INVOICE" in read_pdf_res.data["content"]
    assert len(read_pdf_res.evidence_refs) == 1

    # 4. Read CSV
    csv_path = "./env/fileshare/data/AcmeCorp_Oct2026_statement.csv"
    read_csv_res = await tool_registry.execute("files_read_file", {"path": csv_path})
    assert read_csv_res.ok
    assert read_csv_res.data["format"] == "csv"
    assert read_csv_res.data["rows_count"] > 0

    # 5. Write file
    temp_path = "./artifacts/temp_test.txt"
    write_res = await tool_registry.execute(
        "files_write_file", {"path": temp_path, "content": "Tool layer verified."}
    )
    assert write_res.ok

    read_back = await tool_registry.execute("files_read_file", {"path": temp_path})
    assert read_back.ok
    assert read_back.data["content"] == "Tool layer verified."

    # 6. Sandboxing violation check
    unsafe_res = await tool_registry.execute("files_read_file", {"path": "/etc/passwd"})
    assert not unsafe_res.ok
    assert "Access denied" in unsafe_res.error


# ---------------------------------------------------------------------------
# 3. HTTP Tool & Security Guardrails
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_http_tool_security_and_execution():
    """Verify HTTP requests work on mock apps, but strictly block /__truth to prevent agent cheating."""
    # 1. Legitimate allowed call to mock ERP
    valid_res = await tool_registry.execute(
        "http_request", {"method": "GET", "url": f"{BASE_URL}/erp/vendors"}
    )
    assert valid_res.ok
    assert valid_res.data["status_code"] == 200
    assert valid_res.data["is_json"]
    assert "vendors" in valid_res.data["json"]

    # 2. Strict Security Guardrail: Attempting to call ground-truth endpoint /__truth is denied
    cheat_res = await tool_registry.execute(
        "http_request", {"method": "GET", "url": f"{BASE_URL}/__truth/state"}
    )
    assert not cheat_res.ok
    assert "Access denied" in cheat_res.error
    assert "/__truth" in cheat_res.error

    # 3. Host allowlist check (external hosts denied)
    external_res = await tool_registry.execute(
        "http_request", {"method": "GET", "url": "https://api.external.com/data"}
    )
    assert not external_res.ok
    assert "Access denied" in external_res.error


# ---------------------------------------------------------------------------
# 4. Mail Tools Suite
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_mail_tools_suite():
    """Verify search_mail, read_mail, and save_attachment."""
    # 1. Search mail
    search_res = await tool_registry.execute("mail_search", {"query": "Globex"})
    assert search_res.ok
    assert search_res.data["count"] > 0
    email_id = search_res.data["emails"][0]["id"]

    # 2. Read mail
    read_res = await tool_registry.execute("mail_read", {"email_id": email_id})
    assert read_res.ok
    assert "Globex Systems" in read_res.data["email"]["subject"]

    # 3. Save attachment
    save_res = await tool_registry.execute("mail_save_attachment", {"email_id": email_id})
    assert save_res.ok
    assert os.path.exists(save_res.data["saved_path"])
    assert save_res.data["filename"] == "INV-GLOBEX-901.pdf"


# ---------------------------------------------------------------------------
# 5. Human-in-the-Loop Tools
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_human_tools_suite():
    """Verify ask_user and request_approval emit structured pause signals."""
    ask_res = await tool_registry.execute(
        "ask_user",
        {
            "question": "Which company do you mean?",
            "options": ["Acme Corp Ltd", "Acme Corp Inc"],
        },
    )
    assert ask_res.ok
    assert ask_res.data["signal"] == "WAITING_USER"
    assert len(ask_res.data["options"]) == 2

    approval_res = await tool_registry.execute(
        "request_approval",
        {
            "action": "Submit bill for INV-STARK-105",
            "reason": "Amount of INR 75,000 exceeds INR 50,000 threshold",
            "risk_tier": "IRREVERSIBLE_WRITE",
        },
    )
    assert approval_res.ok
    assert approval_res.data["signal"] == "WAITING_APPROVAL"


# ---------------------------------------------------------------------------
# 6. Live Playwright Browser Tools Flow
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_live_browser_navigation_snapshot_and_interaction():
    """
    Test Playwright browser tools driving the mock apps:
    1. Navigate to /portal/login
    2. Snapshot and verify numbered reference IDs
    3. Login via type_text and click
    4. Screenshot
    5. Download invoice PDF via browser
    """
    # 1. Open URL
    open_res = await tool_registry.execute("browser_open_url", {"url": f"{BASE_URL}/portal/login"})
    assert open_res.ok
    assert "SupplierConnect" in open_res.data["snapshot"]

    # 2. Type credentials using reference ID or selector
    type_user = await tool_registry.execute(
        "browser_type_text", {"target": "#email", "text": "supplier@acme.com"}
    )
    assert type_user.ok

    type_pass = await tool_registry.execute(
        "browser_type_text", {"target": "#password", "text": "portal_pass123"}
    )
    assert type_pass.ok

    # 3. Click login button
    click_res = await tool_registry.execute("browser_click", {"target": "#portal-submit-btn"})
    assert click_res.ok
    assert "/portal/invoices" in click_res.data["url"]

    # 4. Snapshot
    snap_res = await tool_registry.execute("browser_snapshot", {})
    assert snap_res.ok
    snapshot_text = snap_res.data["snapshot"]
    assert "Published Invoices" in snapshot_text
    assert "[e" in snapshot_text  # Contains assigned element refs

    # 5. Take screenshot
    screen_res = await tool_registry.execute("browser_screenshot", {"name": "test_portal"})
    assert screen_res.ok
    assert os.path.exists(screen_res.data["screenshot_path"])

    # 6. Download file using browser
    download_res = await tool_registry.execute("browser_download_file", {"target": "Download PDF"})
    assert download_res.ok
    assert os.path.exists(download_res.data["downloaded_file"])

    # Clean up browser session
    await get_browser_manager().close()


# ---------------------------------------------------------------------------
# 7. Browser Ref Resolution Under Renamed Elements Fault
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_browser_renamed_elements_fault_resilience():
    """
    Verify that when the renamed_elements fault is active,
    the browser tools interact successfully via semantic text labels and numbered refs.
    """
    # 1. Activate UI drift fault via API
    fault_manager.update_faults({"renamed_elements": True})

    # 2. Login to ERP
    await tool_registry.execute("browser_open_url", {"url": f"{BASE_URL}/erp/login"})
    await tool_registry.execute("browser_type_text", {"target": "#username", "text": "admin"})
    await tool_registry.execute("browser_type_text", {"target": "#password", "text": "erp_pass123"})
    await tool_registry.execute("browser_click", {"target": "#erp-submit-btn"})

    # 3. Navigate to New Bill
    await tool_registry.execute("browser_open_url", {"url": f"{BASE_URL}/erp/bills/new"})
    snap_res = await tool_registry.execute("browser_snapshot", {})
    assert snap_res.ok
    snap_text = snap_res.data["snapshot"]

    # Verify drifted element names are present
    assert 'name="supplier_entity"' in snap_text
    assert 'name="total_payable"' in snap_text
    assert 'text="Process & Post Bill"' in snap_text

    # 4. Fill form using visible labels / semantic targets
    await tool_registry.execute(
        "browser_type_text", {"target": "Supplier Entity Name", "text": "Stark Logistics Pvt Ltd"}
    )
    await tool_registry.execute(
        "browser_type_text", {"target": "Bill Reference Number", "text": "INV-STARK-RESILIENT-99"}
    )
    await tool_registry.execute(
        "browser_type_text", {"target": "Total Amount Payable", "text": "62000.00"}
    )
    await tool_registry.execute(
        "browser_type_text", {"target": "Payment Due Date", "text": "2026-11-30"}
    )

    # 5. Upload attachment
    test_pdf = "./env/fileshare/data/invoices/INV-GLOBEX-901.pdf"
    upload_res = await tool_registry.execute(
        "browser_upload_file", {"target": "Original Document File", "file_path": test_pdf}
    )
    assert upload_res.ok

    # 6. Click drifted submit button
    submit_click = await tool_registry.execute("browser_click", {"target": "Process & Post Bill"})
    assert submit_click.ok
    assert "/erp/bills" in submit_click.data["url"]
    assert "INV-STARK-RESILIENT-99" in submit_click.data["snapshot"]

    await get_browser_manager().close()
