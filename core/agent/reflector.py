from __future__ import annotations

import logging
import re
from typing import Any

from core.agent.models import AgentAction, Goal, Intent, ObservationType, Reflection
from core.llm.client import LLMClient
from core.llm.models import LLMMessage
from core.tools.registry import ToolResult

logger = logging.getLogger(__name__)

REFLECTOR_SYSTEM_PROMPT = """You are CentrAlign's Reflector.
Your job is to analyze the result of a tool execution and update the agent's understanding.

Analyze:
1. OBSERVATION TYPE:
   - "progress": The tool succeeded and moved the task forward.
   - "no_op": The action succeeded, but nothing changed (e.g. clicked empty area, reloaded identical page).
   - "error": The tool execution failed or threw an error message.
   - "surprise": An unexpected situation occurred (e.g. redirected to login page due to expired session, invoice not found in search results, warning dialog, duplicate rejection).
2. SUMMARY: 1-2 sentence factual summary of what happened.
3. INTENT COMPLETED: Did this action fulfill the expected outcome of the current intent? (true/false)
4. FACTS LEARNED: Key-value facts extracted from the observation (e.g., {"invoice_number": "INV-102", "amount": 45000, "due_date": "2026-11-15", "downloaded_pdf": "/path/to/inv.pdf"}).
5. NEEDS REPLAN: True if the current intent is impossible via this path and requires pivoting to an alternative approach.
"""


class Reflector:
    """Analyzes action outcomes, updates working memory, and detects state shifts."""

    def __init__(self, llm_client: LLMClient | None = None) -> None:
        self.llm = llm_client

    async def reflect(
        self,
        goal: Goal,
        current_intent: Intent | None,
        action: AgentAction,
        result: ToolResult,
        current_memory: dict[str, Any],
    ) -> Reflection:
        data_str = str(result.data or "")
        error_str = str(result.error or "")
        combined_text = f"{data_str} {error_str}".lower()

        # 1. Fault Detection: Duplicate Bill Handling (ERP 409 or banner)
        if "duplicate" in combined_text and (
            "already exists" in combined_text
            or "already entered" in combined_text
            or "detected" in combined_text
            or "reject" in combined_text
        ):
            logger.info("Reflector detected duplicate invoice rejection.")
            inv_match = re.search(r"\b(INV-[A-Z0-9-]+)\b", f"{data_str} {error_str}", re.IGNORECASE)
            dup_facts: dict[str, Any] = {
                "is_duplicate": True,
                "duplicate_detected": True,
            }
            if inv_match:
                dup_facts["invoice_number"] = inv_match.group(1).upper()
            return Reflection(
                observation_type=ObservationType.PROGRESS,
                summary="Invoice already entered in ERP as a duplicate; skipped duplicate creation per policy.",
                intent_completed=True,
                facts_learned=dup_facts,
                suggested_next_action="Finish task reporting that invoice is already recorded in ERP.",
            )

        # 2. Fault Detection: Session Expiry Mid-Task
        if "session has expired" in combined_text or "expired=1" in combined_text or ("session expired" in combined_text):
            logger.warning("Reflector detected session expiry mid-task.")
            return Reflection(
                observation_type=ObservationType.SURPRISE,
                summary="User session has expired. Re-authentication required to continue.",
                intent_completed=False,
                facts_learned={"session_expired": True, "auth_required": True},
                needs_replan=False,
                suggested_next_action="Re-authenticate using stored credentials from company vault.",
            )

        # 3. Fault Detection: Missing Portal Invoice (need source switch)
        if "no invoices matching" in combined_text or "0 invoices found" in combined_text:
            logger.warning("Reflector detected target invoice is missing from portal. Re-planning to alternative source.")
            return Reflection(
                observation_type=ObservationType.SURPRISE,
                summary="Target invoice not found in Vendor Portal. Must switch to alternative source (Mailbox or Shared Files).",
                intent_completed=False,
                facts_learned={"missing_from_portal": True},
                needs_replan=True,
                suggested_next_action="Search Corporate Mailbox or Shared Drive for the missing invoice.",
            )

        # 4. Standard Error Handling
        if not result.ok:
            obs_type = ObservationType.ERROR
            if any(term in error_str.lower() for term in ["session expired", "not found", "404", "duplicate"]):
                obs_type = ObservationType.SURPRISE

            return Reflection(
                observation_type=obs_type,
                summary=f"Tool '{action.tool}' failed: {error_str}",
                intent_completed=False,
                facts_learned={},
                needs_replan=False,
                suggested_next_action="Review error and retry or try alternative selector/approach",
            )

        # 5. If an LLM is provided, perform deep reflection
        if self.llm is not None:
            try:
                obs_data_preview = str(result.data)
                if len(obs_data_preview) > 1500:
                    obs_data_preview = obs_data_preview[:1500] + "... [truncated]"

                user_content = (
                    f"Goal: {goal.outcome}\n"
                    f"Current Intent: {current_intent.description if current_intent else 'None'}\n"
                    f"Expected Outcome: {current_intent.expected_outcome if current_intent else 'None'}\n"
                    f"Action Taken: {action.tool}({action.args})\n"
                    f"Tool Result OK: {result.ok}\n"
                    f"Observation Data: {obs_data_preview}\n"
                    f"Current Memory: {current_memory}\n"
                )

                messages = [
                    LLMMessage(role="system", content=REFLECTOR_SYSTEM_PROMPT),
                    LLMMessage(role="user", content=user_content),
                ]

                response = await self.llm.generate_async(
                    messages=messages,
                    response_model=Reflection,
                    temperature=0.1,
                )
                reflection = response.get_parsed(Reflection)

                # Ensure extracted document fields are preserved in facts_learned
                clean_tool = (action.tool or "").lower()
                if any(k in clean_tool for k in ["read_file", "extract_text", "download"]):
                    text_content = ""
                    if isinstance(result.data, dict) and "content" in result.data:
                        text_content = str(result.data["content"])
                    elif isinstance(result.data, str):
                        text_content = result.data

                    inv_match = re.search(r"\b(INV-[A-Z0-9-]+)\b", text_content, re.IGNORECASE)
                    if inv_match and "invoice_number" not in reflection.facts_learned:
                        reflection.facts_learned["invoice_number"] = inv_match.group(1).upper()

                    amt_match = re.search(r"(?:Total(?:\s+Amount|\s+Payable)?|Amount(?:\s+Due)?|Grand\s+Total(?:\s+Payable)?|Balance\s+Due)[:\s]*(?:[A-Z]{3}|[$€₹])?\s*([\d,]+(?:\.\d{2})?)", text_content, re.IGNORECASE)
                    if amt_match and "amount" not in reflection.facts_learned:
                        clean_amt = re.sub(r"[^\d.]", "", amt_match.group(1))
                        try:
                            reflection.facts_learned["amount"] = float(clean_amt)
                        except ValueError:
                            pass

                    date_match = re.search(r"(?:Due\s+Date|Payment\s+Due(?:\s+Date)?|Due)[:\s]*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", text_content, re.IGNORECASE)
                    if date_match and "due_date" not in reflection.facts_learned:
                        reflection.facts_learned["due_date"] = date_match.group(1)

                logger.info(
                    f"Reflected: {reflection.observation_type.value} | Intent Done: {reflection.intent_completed} | "
                    f"Facts: {list(reflection.facts_learned.keys())}"
                )
                return reflection
            except Exception as e:
                logger.warning(f"LLM reflection failed, using heuristic reflection: {e}")

        # 6. Heuristic fallback reflection
        facts: dict[str, Any] = {}
        intent_completed = False
        summary = f"Tool '{action.tool}' succeeded."

        # Field extraction from read_file or text extract
        clean_tool = (action.tool or "").lower()
        if any(k in clean_tool for k in ["read_file", "extract_text"]):
            text_content = ""
            if isinstance(result.data, dict) and "content" in result.data:
                text_content = str(result.data["content"])
            elif isinstance(result.data, str):
                text_content = result.data

            # Regex search for invoice number
            inv_match = re.search(r"\b(INV-[A-Z0-9-]+)\b", text_content, re.IGNORECASE)
            if inv_match:
                facts["invoice_number"] = inv_match.group(1).upper()

            # Regex search for amount
            amt_match = re.search(r"(?:Total(?:\s+Amount|\s+Payable)?|Amount(?:\s+Due)?|Grand\s+Total(?:\s+Payable)?|Balance\s+Due)[:\s]*(?:[A-Z]{3}|[$€₹])?\s*([\d,]+(?:\.\d{2})?)", text_content, re.IGNORECASE)
            if amt_match:
                clean_amt = re.sub(r"[^\d.]", "", amt_match.group(1))
                try:
                    facts["amount"] = float(clean_amt)
                except ValueError:
                    pass

            # Regex search for due date
            date_match = re.search(r"(?:Due\s+Date|Payment\s+Due)[:\s]*(\d{4}-\d{2}-\d{2}|\d{2}/\d{2}/\d{4})", text_content, re.IGNORECASE)
            if date_match:
                facts["due_date"] = date_match.group(1)

            if current_intent and any(k in current_intent.description.lower() for k in ["extract", "read", "parse"]):
                intent_completed = True
                summary = f"Extracted details: {list(facts.keys())}"

        if isinstance(result.data, dict):
            if "downloaded_to" in result.data or "downloaded_file" in result.data:
                file_path = result.data.get("downloaded_to") or result.data.get("downloaded_file")
                facts["downloaded_file"] = file_path
                facts["file_name"] = result.data.get("filename")
                summary = f"Downloaded file to {file_path}."
                if current_intent and "download" in current_intent.description.lower():
                    intent_completed = True
            elif "bill_id" in result.data or "status" in result.data:
                facts.update(result.data)
                summary = f"ERP operation completed: {result.data}"
                if current_intent and any(k in current_intent.description.lower() for k in ["submit", "post", "enter"]):
                    intent_completed = True
        elif isinstance(result.data, str):
            if len(result.data) < 200:
                summary = result.data

        return Reflection(
            observation_type=ObservationType.PROGRESS,
            summary=summary,
            intent_completed=intent_completed,
            facts_learned=facts,
        )
