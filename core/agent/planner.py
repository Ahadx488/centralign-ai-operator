from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from core.agent.models import Goal, Intent, IntentStatus, Plan, Reflection
from core.llm.client import LLMClient
from core.llm.models import LLMMessage

logger = logging.getLogger(__name__)

PLANNER_SYSTEM_PROMPT = """You are CentrAlign's Living Planner.
Your job is to generate a dynamic plan consisting of 3 to 5 high-level INTENTS (milestones) to achieve the user's goal.

Rules for Intents:
1. Intents are semantic milestones, NOT brittle micro-clicks (e.g. "Locate and download invoice PDF", NOT "click e12").
2. Each intent must state a clear expected outcome that can be verified.
3. Keep the plan lean: 3 to 5 intents total.
4. Adapt to the available tools and company procedures provided.
"""

REPLAN_SYSTEM_PROMPT = """You are CentrAlign's Living Planner performing a RE-PLAN.
An unexpected observation, obstacle, or failure occurred during execution.
Your job is to keep completed intents as completed, and adapt the remaining intents with an ALTERNATIVE strategy.

Rules for Re-planning:
1. DO NOT repeat the exact strategy that just failed.
2. If data was missing from one source (e.g. Vendor Portal), pivot to an alternative source (e.g. Corporate Mailbox or Shared Drive).
3. If an error occurred (e.g. session expired, field format rejected), add an intent to resolve the prerequisite (e.g. re-login or format correction).
4. Output the full updated plan with updated pending intents.
"""


class InitialPlanOutput(BaseModel):
    intents: list[Intent] = Field(description="Sequential list of high-level milestone intents.")


class Planner:
    """Generates initial living plans and dynamic re-plans on failures or surprises."""

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm = llm_client

    async def plan(
        self,
        goal: Goal,
        available_tools: list[str],
        company_context: str = "",
    ) -> Plan:
        logger.info(f"Generating initial plan for goal: {goal.outcome[:80]}...")

        user_content = (
            f"Goal: {goal.outcome}\n"
            + "Success Criteria:\n" + "\n".join(f"- {c}" for c in goal.success_criteria) + "\n\n"
            + "Constraints:\n" + "\n".join(f"- {c}" for c in goal.constraints) + "\n\n"
            + "Available Tools:\n" + ", ".join(available_tools) + "\n"
        )
        if company_context:
            user_content += f"\nCompany Context:\n{company_context}\n"

        messages = [
            LLMMessage(role="system", content=PLANNER_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        try:
            response = await self.llm.generate_async(
                messages=messages,
                response_model=InitialPlanOutput,
                temperature=0.2,
            )
            parsed = response.get_parsed(InitialPlanOutput)
            # Ensure IDs and statuses
            intents: list[Intent] = []
            for idx, item in enumerate(parsed.intents, start=1):
                intents.append(
                    Intent(
                        id=f"intent_{idx}",
                        description=item.description,
                        expected_outcome=item.expected_outcome,
                        status=IntentStatus.PENDING,
                        max_retries=3,
                    )
                )
            if intents:
                intents[0].status = IntentStatus.IN_PROGRESS
            return Plan(intents=intents, current_intent_index=0, replan_count=0)
        except Exception as e:
            logger.warning(f"Plan generation failed with LLM, using fallback intents: {e}")
            fallback_intents = [
                Intent(
                    id="intent_1",
                    description=f"Gather required documents and data for: {goal.outcome}",
                    expected_outcome="Relevant documents downloaded or retrieved",
                    status=IntentStatus.IN_PROGRESS,
                ),
                Intent(
                    id="intent_2",
                    description="Extract and validate necessary fields and information",
                    expected_outcome="Required details extracted to working memory",
                    status=IntentStatus.PENDING,
                ),
                Intent(
                    id="intent_3",
                    description="Perform required updates or submissions in target internal system",
                    expected_outcome="Action recorded successfully in target system",
                    status=IntentStatus.PENDING,
                ),
                Intent(
                    id="intent_4",
                    description="Verify the outcome against success criteria",
                    expected_outcome="All success criteria confirmed",
                    status=IntentStatus.PENDING,
                ),
            ]
            return Plan(intents=fallback_intents, current_intent_index=0, replan_count=0)

    async def replan(
        self,
        goal: Goal,
        current_plan: Plan,
        reflection: Reflection,
        working_memory: dict[str, Any],
        last_error: str = "",
        company_context: str = "",
    ) -> Plan:
        logger.info(f"Re-planning triggered (count={current_plan.replan_count + 1}). Error/Reason: {last_error or reflection.summary}")

        completed_intents = [
            i for i in current_plan.intents
            if i.status in {IntentStatus.COMPLETED, IntentStatus.SKIPPED}
        ]

        user_content = (
            f"Goal: {goal.outcome}\n\n"
            f"Already Completed Intents:\n"
            + ("\n".join(f"- {i.description} (status: {i.status.value})" for i in completed_intents) or "None")
            + "\n\n"
            f"Failed / Interrupted Intent: {current_plan.current_intent.description if current_plan.current_intent else 'N/A'}\n"
            f"Obstacle / Error: {last_error or reflection.summary}\n"
            f"Working Memory Facts: {working_memory}\n"
        )
        if company_context:
            user_content += f"\nCompany Context:\n{company_context}\n"

        messages = [
            LLMMessage(role="system", content=REPLAN_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        try:
            response = await self.llm.generate_async(
                messages=messages,
                response_model=InitialPlanOutput,
                temperature=0.2,
            )
            parsed = response.get_parsed(InitialPlanOutput)

            # Combine completed intents with new adapted intents
            combined_intents: list[Intent] = list(completed_intents)
            start_idx = len(combined_intents) + 1
            for offset, new_intent in enumerate(parsed.intents):
                combined_intents.append(
                    Intent(
                        id=f"intent_{start_idx + offset}",
                        description=new_intent.description,
                        expected_outcome=new_intent.expected_outcome,
                        status=IntentStatus.PENDING,
                        max_retries=3,
                    )
                )

            next_index = len(completed_intents)
            if next_index < len(combined_intents):
                combined_intents[next_index].status = IntentStatus.IN_PROGRESS

            return Plan(
                intents=combined_intents,
                current_intent_index=next_index,
                replan_count=current_plan.replan_count + 1,
            )
        except Exception as e:
            logger.warning(f"Replan LLM call failed, appending adaptive recovery intent: {e}")
            new_intents = list(current_plan.intents)
            curr = current_plan.current_intent
            if curr:
                curr.status = IntentStatus.FAILED
                curr.notes = f"Failed: {last_error or reflection.summary}"
            # Add an alternative recovery intent
            alt_intent = Intent(
                id=f"intent_{len(new_intents) + 1}",
                description=f"Try alternative method/source to recover from: {last_error or reflection.summary}",
                expected_outcome="Alternative path executed or escalation",
                status=IntentStatus.IN_PROGRESS,
            )
            new_intents.append(alt_intent)
            return Plan(
                intents=new_intents,
                current_intent_index=len(new_intents) - 1,
                replan_count=current_plan.replan_count + 1,
            )
