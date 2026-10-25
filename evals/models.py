from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field


class EvalTaskVariant(BaseModel):
    task_id: str
    name: str
    category: str
    task: str
    faults: dict[str, Any] = Field(default_factory=dict)
    scenario_id: int | str | None = None
    expected_outcome: str = "COMPLETED"
    requires_approval: bool = False
    requires_clarification: bool = False
    clarification_input: str | None = None
    is_generalization: bool = False
    has_prompt_injection: bool = False
    test_crash_resume: bool = False
    test_learning: bool = False


class EvalTaskResult(BaseModel):
    task_id: str
    name: str
    category: str
    agent_claimed_done: bool
    verifier_outcome: str  # PASS, FAIL, UNVERIFIABLE, ESCALATED
    verified_success: bool
    is_false_completion: bool  # agent claimed done when verifier outcome == FAIL
    steps_count: int
    tokens_used: int
    duration_sec: float
    cost_usd: float
    fault_recovered: bool | None = None
    clarification_precision_ok: bool = True
    approval_precision_ok: bool = True
    policy_blocked_injection: bool | None = None
    error: str | None = None


class EvalReport(BaseModel):
    total_tasks: int
    passed_tasks: int
    verified_success_rate: float
    false_completion_rate: float  # MUST BE 0.0%
    fault_recovery_rate: float
    generalization_pass_rate: float
    avg_steps_per_task: float
    avg_tokens_per_task: float
    avg_duration_sec: float
    avg_cost_usd: float
    clarification_precision: float
    approval_precision: float
    task_results: list[EvalTaskResult]
    timestamp: str = Field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )

    def to_markdown_table(self) -> str:
        """Render results as a clean Markdown table per Section 7 and 8."""
        lines = [
            "### Evaluation Suite Summary (§7 Metrics)",
            "",
            f"- **Verified Success Rate (Verifier-Confirmed)**: **{self.verified_success_rate * 100:.1f}%** ({self.passed_tasks}/{self.total_tasks})",
            f"- **False-Completion Rate**: **{self.false_completion_rate * 100:.1f}%** (Target: 0.0% — Agent claims completion without independent verifier confirmation)",
            f"- **Fault Recovery Rate**: **{self.fault_recovery_rate * 100:.1f}%** across injected faults",
            f"- **Generalization Pass Rate**: **{self.generalization_pass_rate * 100:.1f}%** on unseen task types (zero agent code change)",
            f"- **Clarification & Approval Precision**: **{self.clarification_precision * 100:.1f}%** clarification / **{self.approval_precision * 100:.1f}%** approval",
            f"- **Average Steps per Task**: **{self.avg_steps_per_task:.1f}** steps",
            f"- **Average Tokens per Task**: **{self.avg_tokens_per_task:.0f}** tokens (Avg Cost: **${self.avg_cost_usd:.4f}**)",
            "",
            "#### Detailed Scenario & Fault Variant Scorecard",
            "",
            "| ID | Task / Scenario | Category | Fault Variant | Steps | Tokens | Time (s) | Verifier Status | Outcome |",
            "|---|---|---|---|---|---|---|---|---|",
        ]

        for r in self.task_results:
            status_icon = "✅ PASS" if r.verified_success else ("⚠️ HOLD" if r.verifier_outcome == "ESCALATED" else "❌ FAIL")
            fc_flag = " 🚨 FALSE COMPLETION" if r.is_false_completion else ""
            lines.append(
                f"| `{r.task_id}` | {r.name} | `{r.category}` | "
                f"{r.fault_recovered is not None and 'Active' or 'None'} | "
                f"{r.steps_count} | {r.tokens_used:,} | {r.duration_sec:.1f}s | "
                f"`{r.verifier_outcome}` | {status_icon}{fc_flag} |"
            )

        return "\n".join(lines)
