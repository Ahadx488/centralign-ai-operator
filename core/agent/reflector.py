from __future__ import annotations

import logging
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
   - "surprise": An unexpected situation occurred (e.g. redirected to login page due to expired session, invoice not found in search results, warning dialog).
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
        # 1. Quick heuristic fast-path for obvious outcomes
        if not result.ok:
            error_str = str(result.error or "Unknown error")
            obs_type = ObservationType.ERROR
            needs_replan = False
            if any(term in error_str.lower() for term in ["session expired", "not found", "404", "duplicate"]):
                obs_type = ObservationType.SURPRISE

            return Reflection(
                observation_type=obs_type,
                summary=f"Tool '{action.tool}' failed: {error_str}",
                intent_completed=False,
                facts_learned={},
                needs_replan=needs_replan,
                suggested_next_action="Review error and retry or try alternative selector/approach",
            )

        # 2. If an LLM is provided, perform deep reflection
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
                logger.info(
                    f"Reflected: {reflection.observation_type.value} | Intent Done: {reflection.intent_completed} | "
                    f"Facts: {list(reflection.facts_learned.keys())}"
                )
                return reflection
            except Exception as e:
                logger.warning(f"LLM reflection failed, using heuristic reflection: {e}")

        # 3. Heuristic fallback reflection
        facts: dict[str, Any] = {}
        intent_completed = False
        summary = f"Tool '{action.tool}' succeeded."

        if isinstance(result.data, dict):
            if "downloaded_to" in result.data:
                facts["downloaded_file"] = result.data["downloaded_to"]
                facts["file_name"] = result.data.get("file_name")
                summary = f"Downloaded file to {result.data['downloaded_to']}."
                if current_intent and "download" in current_intent.description.lower():
                    intent_completed = True
            elif "bill_id" in result.data or "status" in result.data:
                facts.update(result.data)
                summary = f"ERP operation completed: {result.data}"
                if current_intent and ("submit" in current_intent.description.lower() or "enter" in current_intent.description.lower()):
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
