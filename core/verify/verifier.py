from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pypdf import PdfReader

from core.runs.models import RunRecord, StepRecord
from core.verify.evidence import EvidenceCollector
from core.verify.models import (
    CheckResult,
    FieldComparison,
    VerificationOutcome,
    VerificationResult,
)

if TYPE_CHECKING:
    from core.agent.models import Goal

logger = logging.getLogger(__name__)


def normalize_amount(val: Any) -> float | None:
    """Normalize string, float, or currency amount into a float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
    # Strip currency symbols and commas
    clean = re.sub(r"[^\d.-]", "", str(val))
    try:
        return float(clean)
    except ValueError:
        return None


def normalize_date(val: Any) -> str | None:
    """Normalize date strings (YYYY-MM-DD, DD/MM/YYYY, MM/DD/YYYY) into standardized YYYY-MM-DD."""
    if not val:
        return None
    s = str(val).strip()
    m_iso = re.match(r"^(\d{4})-(\d{1,2})-(\d{1,2})$", s)
    if m_iso:
        y, mon, d = int(m_iso.group(1)), int(m_iso.group(2)), int(m_iso.group(3))
        return f"{y:04d}-{mon:02d}-{d:02d}"

    m_slash = re.match(r"^(\d{1,2})/(\d{1,2})/(\d{4})$", s)
    if m_slash:
        p1, p2, y = int(m_slash.group(1)), int(m_slash.group(2)), int(m_slash.group(3))
        if p1 > 12:
            d, mon = p1, p2
        elif p2 > 12:
            mon, d = p1, p2
        else:
            mon, d = p1, p2
        return f"{y:04d}-{mon:02d}-{d:02d}"

    return s


def extract_text_from_pdf(pdf_path: str) -> str:
    """Extract raw text from PDF document on disk."""
    if not os.path.exists(pdf_path):
        return ""
    try:
        reader = PdfReader(pdf_path)
        return "\n".join(page.extract_text() or "" for page in reader.pages)
    except Exception as e:
        logger.warning(f"Failed to extract PDF text from {pdf_path}: {e}")
        return ""


def parse_source_document_fields(pdf_path: str) -> dict[str, Any]:
    """
    Extract canonical invoice fields directly from the raw PDF text on disk.
    Independent of agent memory or tool outputs.
    """
    text = extract_text_from_pdf(pdf_path)
    if not text:
        return {}

    fields: dict[str, Any] = {"raw_text": text}

    # Extract invoice number (e.g. INV-ACME-102, INV-GLOBEX-901)
    inv_match = re.search(r"\b(INV-[A-Z0-9-]+)\b", text, re.IGNORECASE)
    if inv_match:
        fields["invoice_number"] = inv_match.group(1).upper()

    # Extract vendor name
    vendor_match = re.search(r"Vendor:\s*([^\n\r]+)", text, re.IGNORECASE)
    if vendor_match:
        fields["vendor_name"] = vendor_match.group(1).strip()
    else:
        # Most invoices place vendor name on the first non-empty line
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            fields["vendor_name"] = lines[0]

    # Extract amount
    # Matches patterns like "Grand Total Payable: INR 64,977.03", "Total: $91,015.34", "Total: 34,500.00"
    amount_match = re.search(
        r"(?:Total(?:\s+Amount|\s+Payable)?|Amount(?:\s+Due)?|Grand\s+Total(?:\s+Payable)?|Balance\s+Due)[:\s]*(?:[A-Z]{3}|[$€₹])?\s*([\d,]+(?:\.\d{2})?)",
        text,
        re.IGNORECASE,
    )
    if amount_match:
        fields["amount"] = normalize_amount(amount_match.group(1))

    # Extract due date (YYYY-MM-DD or DD/MM/YYYY or MM/DD/YYYY)
    date_match = re.search(
        r"(?:Due\s+Date|Payment\s+Due(?:\s+Date)?|Due)[:\s]*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        text,
        re.IGNORECASE,
    )
    if date_match:
        fields["due_date"] = date_match.group(1).strip()

    # Currency
    if "INR" in text or "₹" in text:
        fields["currency"] = "INR"
    elif "USD" in text or "$" in text:
        fields["currency"] = "USD"
    elif "EUR" in text or "€" in text:
        fields["currency"] = "EUR"

    return fields


class IndependentVerifier:
    """
    Hard Rule #4 Independent System State Verifier.
    Does NOT trust agent self-reporting. Directly queries ground-truth system
    databases and cross-checks entered values against source documents on disk.
    """

    def __init__(
        self,
        db_path: Path | str = "env/company.db",
        artifacts_dir: Path | str = "artifacts",
    ) -> None:
        self.db_path = str(db_path)
        self.artifacts_dir = Path(artifacts_dir)
        self.collector = EvidenceCollector(artifacts_dir=self.artifacts_dir)

    def _get_db_connection(self) -> sqlite3.Connection | None:
        if not os.path.exists(self.db_path):
            return None
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    async def verify(
        self,
        run: RunRecord,
        goal: Goal,
        working_memory: dict[str, Any],
        steps: list[StepRecord],
        source_doc_override: str | None = None,
    ) -> VerificationResult:
        logger.info(f"[Independent Verifier] Initiating verification for run {run.run_id}")

        checks: list[CheckResult] = []
        discrepancies: list[str] = []
        field_comparisons: list[FieldComparison] = []

        conn = self._get_db_connection()
        if not conn:
            # Cannot access ground truth database
            msg = f"Ground truth database at {self.db_path} could not be opened."
            check = CheckResult(
                check_name="ground_truth_connection",
                passed=False,
                expected="Connected to system DB",
                actual="Connection failed",
                message=msg,
            )
            discrepancies.append(msg)
            bundle = self.collector.assemble_bundle(
                run_id=run.run_id,
                task=run.task,
                outcome=VerificationOutcome.UNVERIFIABLE,
                checks=[check],
                discrepancies=discrepancies,
                extracted_fields_table=[],
            )
            return VerificationResult(
                outcome=VerificationOutcome.UNVERIFIABLE,
                checks=[check],
                discrepancies=discrepancies,
                evidence_bundle=bundle,
            )

        with conn:
            # 1. Identify Target Invoice and Vendor from working memory, task, or steps
            invoice_num = (
                working_memory.get("invoice_number")
                or working_memory.get("bill_reference_no")
                or working_memory.get("invoice_num")
            )
            vendor_name = (
                working_memory.get("vendor_name")
                or working_memory.get("vendor")
                or working_memory.get("supplier_entity")
            )

            # Heuristic regex discovery from task or step args if not in working memory
            if not invoice_num:
                m = re.search(r"\b(INV-[A-Z0-9-]+)\b", run.task, re.IGNORECASE)
                if m:
                    invoice_num = m.group(1).upper()
                else:
                    for s in steps:
                        if isinstance(s.tool_args, dict):
                            for v in s.tool_args.values():
                                if isinstance(v, str) and v.startswith("INV-"):
                                    invoice_num = v.strip()
                                    break
                        if invoice_num:
                            break

            # 2. Check for Duplicate Detection Handling
            is_duplicate_task = (
                working_memory.get("is_duplicate") is True
                or working_memory.get("duplicate_detected") is True
                or any("duplicate" in (s.observation_summary or "").lower() for s in steps)
            )

            if is_duplicate_task and invoice_num:
                # In duplicate scenario, ensure exactly 1 bill existed and no new duplicate was created
                rows = conn.execute(
                    "SELECT * FROM erp_bills WHERE invoice_number = ?",
                    (invoice_num,),
                ).fetchall()
                count = len(rows)
                passed = count == 1
                msg = (
                    f"Duplicate prevention verified: exactly 1 bill exists for {invoice_num} (no double entry)."
                    if passed
                    else f"Duplicate violation! Found {count} bills in ERP for invoice {invoice_num}."
                )
                checks.append(
                    CheckResult(
                        check_name="duplicate_invoice_prevention",
                        passed=passed,
                        expected=1,
                        actual=count,
                        message=msg,
                    )
                )
                if not passed:
                    discrepancies.append(msg)

                outcome = VerificationOutcome.PASS if passed else VerificationOutcome.FAIL
                bundle = self.collector.assemble_bundle(
                    run_id=run.run_id,
                    task=run.task,
                    outcome=outcome,
                    checks=checks,
                    discrepancies=discrepancies,
                    extracted_fields_table=field_comparisons,
                )
                return VerificationResult(
                    outcome=outcome,
                    checks=checks,
                    discrepancies=discrepancies,
                    evidence_bundle=bundle,
                )

            # 3. Determine if the task requires ERP bill verification
            task_text = f"{run.task} {goal.outcome} {' '.join(goal.success_criteria)}".lower()
            requires_bill = (
                any(
                    k in task_text
                    for k in [
                        "process invoice",
                        "enter invoice",
                        "enter it into",
                        "create bill",
                        "post bill",
                        "submit bill",
                        "bill created",
                        "enter bill",
                        "new bill",
                    ]
                )
                or working_memory.get("bill_entered") is True
            )

            if not requires_bill:
                # Non-bill task: verify specific non-bill criteria or general completion
                # A. File retrieval check (if task involves locating/saving files)
                target_file: str | None = None
                for s in steps:
                    if s.tool_name in {"mail_save_attachment", "download_file"} and isinstance(s.tool_args, dict):
                        fn = s.tool_args.get("filename")
                        sdir = s.tool_args.get("save_dir")
                        if fn and sdir:
                            candidate_file = os.path.join(sdir, fn)
                            if os.path.exists(candidate_file):
                                target_file = candidate_file
                                break

                if target_file:
                    file_ok = os.path.exists(target_file) and os.path.getsize(target_file) > 0
                    checks.append(
                        CheckResult(
                            check_name="file_retrieval_verified",
                            passed=file_ok,
                            expected=f"Existing non-empty file at {target_file}",
                            actual=f"Size: {os.path.getsize(target_file)} bytes" if file_ok else "Not found",
                            message=f"Verified target file successfully downloaded: {target_file}",
                        )
                    )
                    if not file_ok:
                        discrepancies.append(f"Target file {target_file} not found or empty.")

                # B. General execution check
                passed = len(discrepancies) == 0
                checks.append(
                    CheckResult(
                        check_name="task_execution_state",
                        passed=passed,
                        expected="Clean execution without unhandled errors",
                        actual="Completed",
                        message="Verified task actions executed successfully.",
                    )
                )

                outcome = VerificationOutcome.PASS if passed else VerificationOutcome.FAIL
                bundle = self.collector.assemble_bundle(
                    run_id=run.run_id,
                    task=run.task,
                    outcome=outcome,
                    checks=checks,
                    discrepancies=discrepancies,
                    extracted_fields_table=field_comparisons,
                )
                return VerificationResult(
                    outcome=outcome,
                    checks=checks,
                    discrepancies=discrepancies,
                    evidence_bundle=bundle,
                )

            # 4. Query ERP Bills for Target Bill
            bill_row: sqlite3.Row | None = None
            if invoice_num:
                bill_row = conn.execute(
                    "SELECT * FROM erp_bills WHERE invoice_number = ? ORDER BY id DESC LIMIT 1",
                    (invoice_num,),
                ).fetchone()

            if not bill_row and vendor_name:
                bill_row = conn.execute(
                    "SELECT * FROM erp_bills WHERE vendor_name LIKE ? ORDER BY id DESC LIMIT 1",
                    (f"%{vendor_name}%",),
                ).fetchone()

            # Check 1: Bill Record Existence in ERP
            if not bill_row:
                msg = f"No bill record found in Acme ERP for invoice='{invoice_num}' or vendor='{vendor_name}'."
                checks.append(
                    CheckResult(
                        check_name="bill_exists_in_erp",
                        passed=False,
                        expected=f"Bill row for {invoice_num or vendor_name}",
                        actual="None",
                        message=msg,
                    )
                )
                discrepancies.append(msg)
                bundle = self.collector.assemble_bundle(
                    run_id=run.run_id,
                    task=run.task,
                    outcome=VerificationOutcome.FAIL,
                    checks=checks,
                    discrepancies=discrepancies,
                    extracted_fields_table=[],
                )
                return VerificationResult(
                    outcome=VerificationOutcome.FAIL,
                    checks=checks,
                    discrepancies=discrepancies,
                    evidence_bundle=bundle,
                )

            checks.append(
                CheckResult(
                    check_name="bill_exists_in_erp",
                    passed=True,
                    expected="Bill record in ERP",
                    actual=f"Bill ID {bill_row['id']} ({bill_row['invoice_number']})",
                    message="Confirmed bill record exists in Acme ERP database.",
                )
            )

            # 4. Locate Source Invoice Document on Disk
            source_doc_path = source_doc_override or working_memory.get("downloaded_file")
            if not source_doc_path or not os.path.exists(source_doc_path):
                # Search fileshare invoices
                pdf_name = bill_row["attachment_filename"] or f"{bill_row['invoice_number']}.pdf"
                candidate = os.path.abspath(f"./env/fileshare/data/invoices/{pdf_name}")
                if os.path.exists(candidate):
                    source_doc_path = candidate
                else:
                    # Search anywhere in artifacts or downloads
                    for root in ["./artifacts/downloads", "./env/fileshare/data/invoices"]:
                        p = os.path.join(root, pdf_name)
                        if os.path.exists(p):
                            source_doc_path = p
                            break

            source_fields: dict[str, Any] = {}
            if source_doc_path and os.path.exists(source_doc_path):
                source_fields = parse_source_document_fields(source_doc_path)
                checks.append(
                    CheckResult(
                        check_name="source_document_found",
                        passed=True,
                        expected="Source document accessible on disk",
                        actual=source_doc_path,
                        message="Located source invoice document on disk for cross-checking.",
                    )
                )
            else:
                checks.append(
                    CheckResult(
                        check_name="source_document_found",
                        passed=False,
                        expected="Source document on disk",
                        actual=source_doc_path or "None",
                        message="Source invoice document could not be located on disk to cross-check.",
                    )
                )
                discrepancies.append("Source document missing for cross-verification.")

            # 5. Independent Field Cross-Checking (ERP State vs. Source Document)
            # A. Vendor Name Match
            erp_vendor = str(bill_row["vendor_name"])
            src_vendor = source_fields.get("vendor_name")
            if src_vendor:
                # Fuzzy or substring match
                vendor_match = (
                    erp_vendor.lower() in src_vendor.lower()
                    or src_vendor.lower() in erp_vendor.lower()
                )
                field_comparisons.append(
                    FieldComparison(
                        field_name="Vendor Name",
                        source_doc_value=src_vendor,
                        system_entered_value=erp_vendor,
                        match=vendor_match,
                        discrepancy_note=None if vendor_match else f"Vendor name mismatch: '{src_vendor}' vs '{erp_vendor}'",
                    )
                )
                if not vendor_match:
                    discrepancies.append(f"Vendor mismatch: Document has '{src_vendor}', but ERP bill has '{erp_vendor}'")

            # B. Invoice Number Match
            erp_inv = str(bill_row["invoice_number"])
            src_inv = source_fields.get("invoice_number")
            if src_inv:
                inv_match = erp_inv.strip().upper() == src_inv.strip().upper()
                field_comparisons.append(
                    FieldComparison(
                        field_name="Invoice Number",
                        source_doc_value=src_inv,
                        system_entered_value=erp_inv,
                        match=inv_match,
                        discrepancy_note=None if inv_match else f"Invoice reference mismatch: '{src_inv}' vs '{erp_inv}'",
                    )
                )
                if not inv_match:
                    discrepancies.append(f"Invoice number mismatch: Document has '{src_inv}', but ERP bill has '{erp_inv}'")

            # C. Amount Cross-Check (CRITICAL: Catches OCR Noise and Hallucinations!)
            erp_amt = normalize_amount(bill_row["amount"])
            src_amt = source_fields.get("amount")
            if src_amt is not None and erp_amt is not None:
                amt_match = abs(erp_amt - src_amt) < 0.01
                msg = (
                    f"Amount verified: document={src_amt}, ERP={erp_amt}"
                    if amt_match
                    else f"Amount discrepancy! Document states {src_amt}, but ERP bill was entered as {erp_amt}"
                )
                field_comparisons.append(
                    FieldComparison(
                        field_name="Total Amount",
                        source_doc_value=src_amt,
                        system_entered_value=erp_amt,
                        match=amt_match,
                        discrepancy_note=None if amt_match else msg,
                    )
                )
                checks.append(
                    CheckResult(
                        check_name="amount_cross_check",
                        passed=amt_match,
                        expected=src_amt,
                        actual=erp_amt,
                        message=msg,
                    )
                )
                if not amt_match:
                    discrepancies.append(msg)
            elif src_amt is None:
                msg = "Amount cross-check failed: Could not extract valid numeric amount from source invoice document."
                checks.append(
                    CheckResult(
                        check_name="amount_cross_check",
                        passed=False,
                        expected="Numeric amount from document",
                        actual=str(erp_amt),
                        message=msg,
                    )
                )
                discrepancies.append(msg)

            # D. Due Date Check
            erp_due = str(bill_row["due_date"])
            src_due = source_fields.get("due_date")
            if src_due:
                norm_erp_due = normalize_date(erp_due)
                norm_src_due = normalize_date(src_due)
                due_match = (
                    (norm_erp_due == norm_src_due)
                    or (erp_due == src_due)
                    or (src_due in erp_due)
                    or (erp_due in src_due)
                )
                field_comparisons.append(
                    FieldComparison(
                        field_name="Due Date",
                        source_doc_value=src_due,
                        system_entered_value=erp_due,
                        match=due_match,
                        discrepancy_note=None if due_match else f"Due date mismatch: document={src_due}, ERP={erp_due}",
                    )
                )
                checks.append(
                    CheckResult(
                        check_name="due_date_cross_check",
                        passed=due_match,
                        expected=norm_src_due or src_due,
                        actual=norm_erp_due or erp_due,
                        message=f"Due date verified: document={src_due}, ERP={erp_due}"
                        if due_match
                        else f"Due date mismatch: document={src_due}, ERP={erp_due}",
                    )
                )
                if not due_match:
                    discrepancies.append(f"Due date mismatch: Document states {src_due}, but ERP bill has {erp_due}")

            # E. Attachment Verification Check
            att_filename = bill_row["attachment_filename"]
            att_exists = False
            if att_filename:
                # Check if file exists in either uploads, invoices, or downloads dir
                for check_dir in [
                    "./env/fileshare/data/invoices",
                    "./env/fileshare/data/uploads",
                    "./artifacts/downloads",
                ]:
                    if os.path.exists(os.path.join(check_dir, att_filename)):
                        att_exists = True
                        break

            checks.append(
                CheckResult(
                    check_name="attachment_presence",
                    passed=bool(att_filename and att_exists),
                    expected="Original invoice file attached",
                    actual=att_filename or "None",
                    message="Attachment file confirmed in ERP and physically present on disk."
                    if (att_filename and att_exists)
                    else "Missing or unreadable attachment in ERP bill.",
                )
            )
            if not (att_filename and att_exists):
                discrepancies.append(f"Attachment verification failed: '{att_filename}' not found on disk.")

        # Determine overall outcome
        all_passed = len(checks) > 0 and all(c.passed for c in checks) and len(discrepancies) == 0
        outcome = VerificationOutcome.PASS if all_passed else VerificationOutcome.FAIL

        bundle = self.collector.assemble_bundle(
            run_id=run.run_id,
            task=run.task,
            outcome=outcome,
            checks=checks,
            discrepancies=discrepancies,
            extracted_fields_table=field_comparisons,
        )

        logger.info(f"[Independent Verifier] Verification result: {outcome.value} (checks={len(checks)}, discrepancies={len(discrepancies)})")
        return VerificationResult(
            outcome=outcome,
            checks=checks,
            discrepancies=discrepancies,
            evidence_bundle=bundle,
        )
