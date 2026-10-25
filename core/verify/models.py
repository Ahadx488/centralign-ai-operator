from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class VerificationOutcome(str, Enum):
    PASS = "PASS"
    FAIL = "FAIL"
    UNVERIFIABLE = "UNVERIFIABLE"


class FieldComparison(BaseModel):
    """Detailed side-by-side comparison of a field extracted from source vs entered in target system."""
    field_name: str
    source_doc_value: Any
    system_entered_value: Any
    match: bool
    discrepancy_note: str | None = None


class CheckResult(BaseModel):
    """Result of an individual independent verification check."""
    check_name: str
    passed: bool
    expected: Any
    actual: Any
    message: str


class EvidenceBundle(BaseModel):
    """
    Structured cryptographic and system state evidence bundle supporting the verification decision.
    """
    run_id: str
    task: str
    outcome: VerificationOutcome
    discrepancies: list[str] = Field(default_factory=list)
    checks: list[CheckResult] = Field(default_factory=list)
    extracted_fields_table: list[FieldComparison] = Field(default_factory=list)
    screenshots: list[str] = Field(default_factory=list)
    artifacts: list[str] = Field(default_factory=list)
    system_state_before: dict[str, Any] = Field(default_factory=dict)
    system_state_after: dict[str, Any] = Field(default_factory=dict)
    timestamp: str = Field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_markdown(self) -> str:
        """Render the evidence bundle as a clean, audit-ready Markdown report."""
        lines: list[str] = [
            f"# Verification Evidence Report — Run `{self.run_id}`",
            f"**Outcome:** `{self.outcome.value}`  ",
            f"**Timestamp:** `{self.timestamp}`  ",
            f"**Task:** {self.task}\n",
            "## 1. Independent Verification Checks",
            "| Check Name | Status | Expected | Actual | Details |",
            "|---|---|---|---|---|",
        ]

        for c in self.checks:
            status = "✅ PASS" if c.passed else "❌ FAIL"
            lines.append(f"| {c.check_name} | {status} | `{c.expected}` | `{c.actual}` | {c.message} |")

        if self.extracted_fields_table:
            lines.extend([
                "\n## 2. Source Document vs. System Entered Values",
                "| Field | Source Doc Value | System Entered Value | Match | Notes |",
                "|---|---|---|---|---|",
            ])
            for f in self.extracted_fields_table:
                match_str = "✅ YES" if f.match else "❌ MISMATCH"
                note = f.discrepancy_note or "Consistent"
                lines.append(f"| **{f.field_name}** | `{f.source_doc_value}` | `{f.system_entered_value}` | {match_str} | {note} |")

        if self.discrepancies:
            lines.extend([
                "\n## 3. Discrepancies & Anomalies Detected",
                *(f"- ⚠️ {d}" for d in self.discrepancies),
            ])

        if self.screenshots:
            lines.extend([
                "\n## 4. Key Step Screenshots",
                *(f"- Screenshot: `{s}`" for s in self.screenshots),
            ])

        return "\n".join(lines)


class VerificationResult(BaseModel):
    """Overall outcome of the independent verifier execution."""
    outcome: VerificationOutcome
    checks: list[CheckResult] = Field(default_factory=list)
    discrepancies: list[str] = Field(default_factory=list)
    evidence_bundle: EvidenceBundle
