from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from core.agent.models import Goal
from core.runs.models import RunRecord, StepRecord
from core.verify.models import VerificationOutcome
from core.verify.verifier import IndependentVerifier


@pytest.mark.asyncio
async def test_negative_verifier_wrong_erp_amount(tmp_path: Path) -> None:
    """
    Negative Verifier Test (Enforces Hard Rule #4):
    If an agent incorrectly records an invoice with amount ₹34,000.00 in ERP
    while the actual source document on disk specifies 91,015.34,
    the Independent Verifier must independently cross-check the real system state,
    catch the discrepancy, and return FAIL (blocking false completion).
    """
    db_path = "env/company.db"
    assert Path(db_path).exists(), f"Mock database not found at {db_path}"

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DELETE FROM erp_bills WHERE invoice_number = 'INV-ACME-106'")
        conn.execute(
            """
            INSERT INTO erp_bills (vendor_name, invoice_number, amount, currency, due_date, attachment_filename, status, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, datetime('now'))
            """,
            ("Acme Corp Ltd", "INV-ACME-106", 34000.00, "EUR", "2026-10-28", "INV-ACME-106.pdf", "entered"),
        )

    verifier = IndependentVerifier(
        db_path=db_path,
        artifacts_dir=tmp_path / "artifacts",
    )

    run = RunRecord(
        task="Find invoice INV-ACME-106 and enter into Acme ERP.",
    )
    goal = Goal(
        outcome="Find invoice INV-ACME-106 and enter into Acme ERP.",
        success_criteria=[
            "Bill record created in Acme ERP matching source invoice document exactly",
        ],
    )
    working_memory = {
        "vendor_name": "Acme Corp Ltd",
        "invoice_number": "INV-ACME-106",
        "amount": 34000.00,  # Agent claims 34,000 entered
    }
    steps = [
        StepRecord(
            run_id=run.run_id,
            step_index=1,
            thought="Entering wrong amount 34000 into ERP form",
            tool_name="browser_upload_file",
            tool_args={"file_path": "env/fileshare/data/invoices/INV-ACME-106.pdf"},
            observation_summary="File uploaded and bill submitted with amount 34000.00",
        )
    ]

    try:
        verif_res = await verifier.verify(
            run=run,
            goal=goal,
            working_memory=working_memory,
            steps=steps,
        )

        print(f"\n[NEGATIVE TEST] Verifier Outcome: {verif_res.outcome.value}")
        print(f"[NEGATIVE TEST] Discrepancies: {verif_res.discrepancies}")
        for c in verif_res.checks:
            print(f"  - Check '{c.check_name}': passed={c.passed}, expected={c.expected}, actual={c.actual}")

        # Assertions
        assert verif_res.outcome == VerificationOutcome.FAIL, f"Expected FAIL, got {verif_res.outcome}"
        assert len(verif_res.discrepancies) > 0, "Expected at least one discrepancy"
        assert any("mismatch" in d.lower() or "discrepancy" in d.lower() for d in verif_res.discrepancies), (
            f"Expected amount mismatch in discrepancies: {verif_res.discrepancies}"
        )
        assert any(not c.passed for c in verif_res.checks), "Expected at least one failed check"

    finally:
        with conn:
            conn.execute("DELETE FROM erp_bills WHERE invoice_number = 'INV-ACME-106'")
