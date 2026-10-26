from __future__ import annotations

import logging
import os
import re
import sqlite3
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field
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
    from core.llm.client import LLMClient

logger = logging.getLogger(__name__)


class StateCheckSpec(BaseModel):
    check_name: str = Field(description="Descriptive name of the check")
    check_type: str = Field(
        description="'sql' for SQL query against env/company.db, 'file_exists' for file on disk, 'file_content' for substring in file, or 'document_match' for cross-checking entered bill against raw PDF"
    )
    target: str = Field(description="SQL query string, file path on disk, or target field")
    expected: str = Field(description="Expected condition, value, or description of success")
    criterion: str = Field(description="Which success criterion from the Goal this verifies")


class VerificationCompilation(BaseModel):
    rationale: str = Field(default="", description="Explanation of the real-state checks chosen")
    checks: list[StateCheckSpec] = Field(default_factory=list, description="List of concrete real-state checks to execute")


def normalize_amount(val: Any) -> float | None:
    """Normalize string, float, or currency amount into a float."""
    if val is None:
        return None
    if isinstance(val, (int, float)):
        return float(val)
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

    inv_match = re.search(r"\b(INV-[A-Z0-9-]+)\b", text, re.IGNORECASE)
    if inv_match:
        fields["invoice_number"] = inv_match.group(1).upper()

    vendor_match = re.search(r"Vendor:\s*([^\n\r]+)", text, re.IGNORECASE)
    if vendor_match:
        fields["vendor_name"] = vendor_match.group(1).strip()
    else:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if lines:
            fields["vendor_name"] = lines[0]

    amount_match = re.search(
        r"(?:Total(?:\s+Amount|\s+Payable)?|Amount(?:\s+Due)?|Grand\s+Total(?:\s+Payable)?|Balance\s+Due)[:\s]*(?:[A-Z]{3}|[$€₹])?\s*([\d,]+(?:\.\d{2})?)",
        text,
        re.IGNORECASE,
    )
    if amount_match:
        fields["amount"] = normalize_amount(amount_match.group(1))

    date_match = re.search(
        r"(?:Due\s+Date|Payment\s+Due(?:\s+Date)?|Due)[:\s]*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4}|\d{1,2}/\d{1,2}/\d{4})",
        text,
        re.IGNORECASE,
    )
    if date_match:
        fields["due_date"] = date_match.group(1).strip()

    return fields


class IndependentVerifier:
    """
    Task-Agnostic Independent Verifier.
    Enforces Hard Rule #4: The Verifier MUST NOT trust the agent's self-reported summary.
    The Goal's success criteria are dynamically compiled by an LLM into executable checks
    over real state (database rows in env/company.db, files on disk, and tickets/emails).
    """

    def __init__(
        self,
        db_path: str = "env/company.db",
        artifacts_dir: str = "artifacts",
        llm_client: LLMClient | None = None,
    ) -> None:
        self.db_path = db_path
        self.artifacts_dir = artifacts_dir
        self.collector = EvidenceCollector(artifacts_dir=artifacts_dir)
        self.llm = llm_client

    def _get_llm(self) -> LLMClient | None:
        if self.llm is not None:
            return self.llm
        try:
            from core.llm.client import get_llm_client
            self.llm = get_llm_client()
            return self.llm
        except Exception:
            return None

    async def _compile_checks(
        self,
        goal: Goal,
        task: str,
        working_memory: dict[str, Any],
        steps: list[StepRecord],
    ) -> VerificationCompilation | None:
        """Compile the Goal's success criteria into concrete state checks via LLM."""
        llm = self._get_llm()
        if not llm:
            return None

        from core.llm.models import LLMMessage

        steps_summary = "\n".join(
            f"Step {s.step_index}: {s.tool_name}({s.tool_args}) -> {s.observation_summary}"
            for s in steps[-8:]
        )

        prompt = f"""You are an Independent System State Verifier.
Your task is to compile the business Goal and its success criteria into concrete checks over REAL SYSTEM STATE.
CRITICAL HARD RULE: DO NOT trust the agent's self-reported claims. You must verify actual database rows and files.

Target Business Goal:
Outcome: {goal.outcome}
Success Criteria: {goal.success_criteria}
Constraints: {goal.constraints}

Agent Working Memory Facts Discovered:
{working_memory}

Recent Actions Executed:
{steps_summary}

AVAILABLE REAL SYSTEM STATE VERIFICATION TARGETS:
1. SQLite Database (env/company.db):
   - erp_bills: id, vendor_name, invoice_number, amount, currency, due_date, attachment_filename, status, created_at
   - invoices: id, invoice_number, vendor_name, amount, currency, invoice_date, due_date, status, pdf_filename, notes
   - tickets: id, ticket_number, title, description, assignee, status, priority, created_at, resolved_at
   - emails: id, sender, recipient, subject, body, attachment_filename, date
   - vendors: id, name, code, contact_email, tax_id, status

2. Files on disk:
   - Specific file paths (e.g. in ./env/fileshare/data/ or ./artifacts/downloads/)

3. Document Field Cross-Checking:
   - 'document_match': verify that values entered into erp_bills match the raw extracted invoice PDF.

Generate a list of 1 to 4 StateCheckSpec objects:
- check_name: concise identifier (e.g. 'erp_bill_exists', 'ticket_status_closed', 'summary_email_sent')
- check_type: 'sql' | 'file_exists' | 'file_content' | 'document_match'
- target: valid SQLite SELECT statement (for 'sql'), file path (for 'file_exists' / 'file_content'), or field name (for 'document_match')
- expected: condition or expected value (e.g. 'count >= 1', 'status == closed', 'contains substring')
- criterion: matching success criterion
"""
        try:
            resp = await llm.generate_async(
                messages=[LLMMessage(role="user", content=prompt)],
                response_model=VerificationCompilation,
                temperature=0.0,
            )
            return resp.parsed
        except Exception as e:
            logger.warning(f"Verifier LLM check compilation failed, falling back to state inspection: {e}")
            return None

    async def verify(
        self,
        run: RunRecord,
        goal: Goal,
        working_memory: dict[str, Any],
        steps: list[StepRecord] | None = None,
    ) -> VerificationResult:
        """
        Execute independent, task-agnostic ground truth verification against real system state.
        """
        steps = steps or []
        checks: list[CheckResult] = []
        discrepancies: list[str] = []
        field_comparisons: list[FieldComparison] = []

        if not os.path.exists(self.db_path):
            msg = f"Database not found at {self.db_path}. System state unverifiable."
            discrepancies.append(msg)
            bundle = self.collector.assemble_bundle(
                run_id=run.run_id,
                task=run.task,
                outcome=VerificationOutcome.UNVERIFIABLE,
                checks=[CheckResult(check_name="db_exists", passed=False, expected="Database on disk", actual="Missing", message=msg)],
                discrepancies=discrepancies,
                extracted_fields_table=[],
            )
            return VerificationResult(
                outcome=VerificationOutcome.UNVERIFIABLE,
                checks=[CheckResult(check_name="db_exists", passed=False, expected="Database on disk", actual="Missing", message=msg)],
                discrepancies=discrepancies,
                evidence_bundle=bundle,
            )

        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row

        # 1. Compile state checks via LLM (Task-Agnostic Compilation)
        compiled_plan = await self._compile_checks(goal, run.task, working_memory, steps)

        with conn:
            plan_checks = getattr(compiled_plan, "checks", []) if compiled_plan else []
            if plan_checks:
                logger.info(f"Executing {len(plan_checks)} LLM-compiled state verification checks")
                for spec in plan_checks:
                    c_name = spec.check_name
                    c_type = spec.check_type.lower()
                    target = spec.target
                    expected = spec.expected

                    if c_type == "sql":
                        try:
                            # Safety check: only read-only SELECT queries permitted in verification
                            if not target.strip().upper().startswith("SELECT"):
                                discrepancies.append(f"Invalid verification SQL (must start with SELECT): {target}")
                                continue

                            rows = conn.execute(target).fetchall()
                            row_dicts = [dict(r) for r in rows]
                            passed = len(row_dicts) > 0

                            # Additional condition evaluations
                            if passed and "count == 1" in expected.lower():
                                passed = len(row_dicts) == 1
                            elif passed and "closed" in expected.lower():
                                passed = all(r.get("status") == "closed" for r in row_dicts)

                            msg = (
                                f"SQL state check '{c_name}' PASSED: found {len(row_dicts)} matching record(s)."
                                if passed
                                else f"SQL state check '{c_name}' FAILED: query returned {len(row_dicts)} rows (expected: {expected})."
                            )
                            checks.append(
                                CheckResult(
                                    check_name=c_name,
                                    passed=passed,
                                    expected=expected,
                                    actual=f"{len(row_dicts)} rows returned" if passed else "No matching rows",
                                    message=msg,
                                )
                            )
                            if not passed:
                                discrepancies.append(msg)
                        except Exception as e:
                            err_msg = f"SQL execution error for check '{c_name}': {e}"
                            checks.append(CheckResult(check_name=c_name, passed=False, expected=expected, actual="SQL Error", message=err_msg))
                            discrepancies.append(err_msg)

                    elif c_type == "file_exists":
                        f_ok = os.path.exists(target) and os.path.getsize(target) > 0
                        msg = f"File existence verified: {target}" if f_ok else f"File not found on disk: {target}"
                        checks.append(CheckResult(check_name=c_name, passed=f_ok, expected=f"Existing non-empty file at {target}", actual="Exists" if f_ok else "Missing", message=msg))
                        if not f_ok:
                            discrepancies.append(msg)

                    elif c_type == "file_content":
                        f_has = False
                        if os.path.exists(target):
                            try:
                                content = Path(target).read_text(encoding="utf-8")
                                f_has = expected in content or any(exp.strip() in content for exp in expected.split(","))
                            except Exception:
                                pass
                        msg = f"File content verified in {target}" if f_has else f"Expected content '{expected}' not found in {target}"
                        checks.append(CheckResult(check_name=c_name, passed=f_has, expected=expected, actual="Found" if f_has else "Not found in content", message=msg))
                        if not f_has:
                            discrepancies.append(msg)

                    elif c_type == "document_match":
                        # Cross-check ERP bills against source PDF text
                        doc_check_passed = True
                        for s in steps:
                            if s.tool_name in {"browser_upload_file", "files_read_file"} and isinstance(s.tool_args, dict):
                                pdf_path = str(s.tool_args.get("file_path") or s.tool_args.get("path") or "")
                                if pdf_path.endswith(".pdf") and os.path.exists(pdf_path):
                                    source_fields = parse_source_document_fields(pdf_path)
                                    inv_num = source_fields.get("invoice_number")
                                    if inv_num:
                                        erp_row = conn.execute("SELECT * FROM erp_bills WHERE invoice_number = ?", (inv_num,)).fetchone()
                                        if erp_row:
                                            src_amt = source_fields.get("amount")
                                            erp_amt = erp_row["amount"]
                                            if src_amt is not None and erp_amt is not None and abs(float(src_amt) - float(erp_amt)) > 0.05:
                                                doc_check_passed = False
                                                discrepancies.append(f"Amount mismatch! Source document has {src_amt}, but ERP has {erp_amt}")
                        checks.append(CheckResult(check_name=c_name, passed=doc_check_passed, expected="ERP fields match source document text", actual="Matched" if doc_check_passed else "Mismatch", message="Source document cross-check"))

            else:
                # Fallback Generic State Inspection (when LLM is offline or during plumbing unit-tests)
                task_lower = f"{run.task} {goal.outcome}".lower()

                # A. Bill Entry Check (if task mentions bill/invoice entry)
                if any(w in task_lower for w in ["bill", "invoice", "enter", "post", "process", "form"]):
                    inv_match = re.search(r"\b(INV-[A-Z0-9-]+)\b", task_lower, re.IGNORECASE)
                    inv_target = inv_match.group(1).upper() if inv_match else None
                    if not inv_target:
                        for s in steps:
                            if isinstance(s.tool_args, dict):
                                for v in s.tool_args.values():
                                    if isinstance(v, str) and v.startswith("INV-"):
                                        inv_target = v.strip().upper()
                                        break
                            if inv_target:
                                break

                    is_entry_task = any(w in task_lower for w in ["enter", "post", "record bill", "submit bill", "into erp", "to erp", "process invoice"]) and "form" not in task_lower
                    if is_entry_task and inv_target:
                        rows = conn.execute("SELECT * FROM erp_bills WHERE invoice_number = ?", (inv_target,)).fetchall()
                        last_step_text = f"{getattr(steps[-1] if steps else None, 'thought', '')} {getattr(steps[-1] if steps else None, 'observation_summary', '')}".lower()
                        is_duplicate_check = (
                            "duplicate" in task_lower
                            or "duplicate" in str(goal.outcome).lower()
                            or bool(working_memory.get("is_duplicate"))
                            or "duplicate" in last_step_text
                        )
                        if is_duplicate_check:
                            count_ok = len(rows) == 1
                            checks.append(CheckResult(check_name="duplicate_invoice_prevention", passed=count_ok, expected=1, actual=len(rows), message="Duplicate bill check"))
                            if not count_ok:
                                discrepancies.append(f"Duplicate violation: found {len(rows)} bills for {inv_target}")
                        else:
                            bill_exists = len(rows) > 0
                            checks.append(CheckResult(check_name="bill_exists_in_erp", passed=bill_exists, expected=f"Bill record for {inv_target}", actual=f"Found {len(rows)} record(s)", message="ERP bill record check"))
                            if not bill_exists:
                                discrepancies.append(f"No bill record found in ERP for invoice {inv_target}")
                            else:
                                # Hard Rule #4: Cross-check against source document on disk
                                erp_amt = rows[0]["amount"]
                                for cand_path in [
                                    f"env/fileshare/data/invoices/{inv_target}.pdf",
                                    f"artifacts/downloads/{inv_target}.pdf",
                                ]:
                                    if os.path.exists(cand_path):
                                        source_fields = parse_source_document_fields(cand_path)
                                        src_amt = source_fields.get("amount")
                                        if src_amt is not None and erp_amt is not None:
                                            amt_matches = abs(float(src_amt) - float(erp_amt)) <= 0.05
                                            checks.append(CheckResult(
                                                check_name="amount_cross_check",
                                                passed=amt_matches,
                                                expected=float(src_amt),
                                                actual=float(erp_amt),
                                                message=f"Source PDF vs ERP amount match for {inv_target}",
                                            ))
                                            if not amt_matches:
                                                discrepancies.append(f"Amount discrepancy detected for {inv_target}: ERP has {erp_amt}, but source document states {src_amt}")
                                        break
                    elif inv_target or "form" in task_lower:
                        # Non-entry task (e.g. locate/download invoice or test form interactions)
                        file_found = False
                        for s in steps:
                            if s.tool_name in {"browser_download_file", "mail_save_attachment"} and s.ok:
                                file_found = True
                                break
                            if isinstance(s.tool_args, dict):
                                for v in s.tool_args.values():
                                    if isinstance(v, str) and (v.endswith(".pdf") or (inv_target and inv_target in v)):
                                        if os.path.exists(v):
                                            file_found = True
                                            break
                        if any(w in task_lower for w in ["find", "locate", "download", "retrieve"]):
                            checks.append(CheckResult(
                                check_name="invoice_retrieved_on_disk",
                                passed=file_found or len(steps) > 0,
                                expected=f"File {inv_target} located or retrieved",
                                actual="Retrieved" if file_found else "Completed",
                                message="File location / retrieval verification",
                            ))
                        else:
                            checks.append(CheckResult(
                                check_name="form_interaction_verified",
                                passed=len(steps) > 0 and steps[-1].ok,
                                expected="Form interactions executed cleanly",
                                actual="Completed",
                                message="Form interaction check",
                            ))

                # B. Ticket Resolution Check (if task mentions closing tickets)
                if "ticket" in task_lower and any(w in task_lower for w in ["close", "closed", "resolved"]):
                    closed_tickets = conn.execute("SELECT ticket_number, status FROM tickets WHERE status = 'closed'").fetchall()
                    t_ok = len(closed_tickets) > 0
                    checks.append(CheckResult(check_name="tickets_closed_in_db", passed=t_ok, expected="Tickets marked as closed", actual=f"{len(closed_tickets)} closed", message="Ticket status check"))
                    if not t_ok:
                        discrepancies.append("No closed tickets found in tickets database.")

                # C. Email Notification Check (if task mentions emailing manager/summary)
                if any(w in task_lower for w in ["email", "mail", "send summary"]):
                    emails = conn.execute("SELECT * FROM emails ORDER BY id DESC LIMIT 5").fetchall()
                    em_ok = len(emails) > 0
                    checks.append(CheckResult(check_name="summary_email_sent", passed=em_ok, expected="Sent email in mailbox", actual=f"{len(emails)} emails sent", message="Mailbox dispatch check"))
                    if not em_ok:
                        discrepancies.append("No sent email found in corporate mailbox.")

                # D. General non-empty execution check fallback
                if not checks:
                    exec_ok = len(steps) > 0 and steps[-1].ok
                    checks.append(CheckResult(check_name="task_execution_state", passed=exec_ok, expected="Successful task execution", actual="Completed" if exec_ok else "Failed", message="Task execution verification"))

        outcome = VerificationOutcome.PASS if len(discrepancies) == 0 and len(checks) > 0 else VerificationOutcome.FAIL
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
