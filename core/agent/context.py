from __future__ import annotations

import json
import logging
from typing import Any

from core.agent.models import Goal, Plan
from core.runs.manager import RunManager
from core.runs.models import StepRecord
from core.tools.registry import ToolResult

logger = logging.getLogger(__name__)

EXECUTOR_SYSTEM_PROMPT = """You are CentrAlign, an autonomous AI Task Worker and Enterprise Operator.
You achieve real-world corporate tasks by interacting with web applications, file systems, mailboxes, and internal APIs.

CORE OPERATIONAL PRINCIPLES:
1. STRICT AUTONOMY & GROUND TRUTH:
   - Perform actions through tools. Never pretend or hallucinate that an action took place.
   - Read system states carefully. Observe returned snapshots, tables, and downloaded documents.
2. LIVING INTENTS & PROGRESSION:
   - Keep track of the active intent in your plan. Take focused steps to accomplish the active intent.
   - If an intent's expected outcome is met, proceed to the next intent.
   - When all intents are accomplished and success criteria are met, finish by returning `is_finish: true`.
3. ADAPTIVE ERROR RECOVERY:
   - If an element is not found, inspect the latest snapshot to see available elements, semantic tags, and refs.
   - If a session expires or you are redirected to a login page, re-authenticate using the credentials in company context.
   - If data is missing from one system, switch to an alternative source (e.g., portal -> mailbox -> shared files).
4. HUMAN INTERACTION & RISK:
   - Use `ask_user` only when genuinely blocked or when encountering ambiguous choices that cannot be verified.
   - Use `request_approval` if an action exceeds spending thresholds (e.g. bills > ₹50,000) or carries high risk.
5. CLEAN COMPLETION:
   - When complete, provide a factual, verifiable summary of what was done (including vendor, invoice number, amount, due date, and submission status).
"""


class ContextManager:
    """
    Manages prompt token budgets, rolling step history summarization,
    and artifact offloading for large tool outputs.
    """

    def __init__(
        self,
        run_manager: RunManager,
        max_recent_steps: int = 5,
        max_observation_chars: int = 2000,
    ) -> None:
        self.run_manager = run_manager
        self.max_recent_steps = max_recent_steps
        self.max_observation_chars = max_observation_chars

    def normalize_observation(
        self,
        run_id: str,
        tool_name: str,
        result: ToolResult,
    ) -> tuple[str, Any]:
        """
        Produce a compact observation summary and optionally offload large raw data to artifacts.
        Returns (summary_string, stored_data_preview).
        """
        if not result.ok:
            error_msg = result.error or "Action failed without explicit error message"
            return f"FAILED: {error_msg}", None

        data = result.data
        if data is None:
            return "SUCCESS (no data returned)", None

        # Handle strings (e.g. snapshots, file text)
        if isinstance(data, str):
            if len(data) > self.max_observation_chars:
                # Offload to artifact
                art = self.run_manager.save_artifact(
                    run_id=run_id,
                    name=f"{tool_name}_output",
                    content=data,
                    summary=f"Full {tool_name} output ({len(data)} chars)",
                    mime_type="text/plain",
                )
                preview = data[:self.max_observation_chars] + f"\n... [Truncated: full output saved in artifact {art.artifact_id}]"
                return f"SUCCESS (truncated): {preview}", {"artifact_id": art.artifact_id, "preview": preview}
            return f"SUCCESS: {data}", data

        # Handle dicts (e.g. download results, search results)
        if isinstance(data, dict):
            serialized = json.dumps(data)
            if len(serialized) > self.max_observation_chars:
                art = self.run_manager.save_artifact(
                    run_id=run_id,
                    name=f"{tool_name}_data",
                    content=serialized,
                    summary=f"Structured {tool_name} response ({len(serialized)} bytes)",
                    mime_type="application/json",
                )
                summary_str = f"SUCCESS: {str(data)[:self.max_observation_chars]}... [Saved in {art.artifact_id}]"
                return summary_str, {"artifact_id": art.artifact_id}
            return f"SUCCESS: {serialized}", data

        # Other types
        return f"SUCCESS: {str(data)[:self.max_observation_chars]}", data

    def build_prompt_context(
        self,
        goal: Goal,
        plan: Plan,
        working_memory: dict[str, Any],
        steps: list[StepRecord],
        company_context: str = "",
    ) -> str:
        """
        Assemble the full prompt context for the executor.
        """
        sections: list[str] = []

        # 1. Goal & Success Criteria
        sections.append("## GOAL & SUCCESS CRITERIA")
        sections.append(f"Outcome: {goal.outcome}")
        if goal.success_criteria:
            sections.append("Success Criteria:")
            for sc in goal.success_criteria:
                sections.append(f"  [ ] {sc}")
        if goal.constraints:
            sections.append("Constraints:")
            for c in goal.constraints:
                sections.append(f"  - {c}")

        # 2. Living Plan
        sections.append("\n## LIVING PLAN (INTENTS)")
        for idx, intent in enumerate(plan.intents):
            status_mark = "✓" if intent.status.value == "completed" else "→" if idx == plan.current_intent_index else " "
            sections.append(f"  [{status_mark}] {intent.id}: {intent.description} (status: {intent.status.value})")
            if idx == plan.current_intent_index:
                sections.append(f"      Expected outcome: {intent.expected_outcome}")
                if intent.retries > 0:
                    sections.append(f"      (Retries on this intent: {intent.retries}/{intent.max_retries})")

        # 3. Working Memory Facts
        sections.append("\n## WORKING MEMORY (VERIFIED FACTS)")
        if working_memory:
            for k, v in working_memory.items():
                sections.append(f"  - {k}: {v}")
        else:
            sections.append("  (No facts accumulated yet)")

        # 4. Company Systems & Procedures Context
        if company_context:
            sections.append(f"\n## COMPANY SYSTEMS & OPERATING CONTEXT\n{company_context}")

        # 5. Execution Timeline / Step History
        sections.append("\n## EXECUTION HISTORY")
        if not steps:
            sections.append("  (No actions executed yet)")
        else:
            # Compress older steps if total steps exceed max_recent_steps
            if len(steps) > self.max_recent_steps:
                older_steps = steps[:-self.max_recent_steps]
                recent_steps = steps[-self.max_recent_steps:]
                older_summary = "; ".join(
                    f"Step {s.step_index}: {s.tool_name} -> {'OK' if s.ok else 'FAIL'}"
                    for s in older_steps
                )
                sections.append(f"  [Earlier Steps 1..{len(older_steps)} Summary]: {older_summary}")
            else:
                recent_steps = steps

            for s in recent_steps:
                sections.append(
                    f"\n  --- Step {s.step_index} ({s.tool_name}) ---"
                )
                if s.thought:
                    sections.append(f"  Thought: {s.thought}")
                sections.append(f"  Action: {s.tool_name}({json.dumps(s.tool_args)})")
                sections.append(f"  Result: {s.observation_summary}")

        sections.append("\n## YOUR NEXT STEP")
        sections.append(
            "Determine the next single action to take to advance the active intent.\n"
            "If all intents are satisfied and success criteria are confirmed, return an action with `is_finish: true`."
        )

        return "\n".join(sections)
