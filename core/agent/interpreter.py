from __future__ import annotations

import logging

from core.agent.models import Goal
from core.llm.client import LLMClient
from core.llm.models import LLMMessage

logger = logging.getLogger(__name__)

INTERPRETER_SYSTEM_PROMPT = """You are CentrAlign's Goal Interpreter.
Your job is to analyze unstructured user tasks and convert them into a structured Goal object.

Guidelines:
1. OUTCOME: A concise definition of the desired end state.
2. SUCCESS CRITERIA: A list of 2-5 concrete, independently verifiable conditions that must be true when complete.
   - Example: "A bill for vendor 'Acme Corp Ltd' is submitted in Acme ERP."
   - Example: "The bill has invoice amount and due date matching the source invoice document."
   - Example: "The source invoice PDF is attached to the bill in ERP."
3. CONSTRAINTS: Key rules or boundaries from company procedures, spending thresholds, or safety rules.
4. UNKNOWNS: Missing information or questions.
5. BLOCKING vs NON-BLOCKING:
   - Set `is_blocking: true` ONLY when the request is critically ambiguous and cannot begin without human clarification (e.g. "Pay the vendor" when no vendor is named).
   - If the task names a specific target (e.g. "Find latest invoice from Acme Corp Ltd and enter it into ERP"), `is_blocking` MUST BE false. Exploration and discovery will resolve the exact invoice number, date, and amount.
"""


class GoalInterpreter:
    """Interprets raw task descriptions into structured Goal models."""

    def __init__(self, llm_client: LLMClient) -> None:
        self.llm = llm_client

    async def interpret(self, task: str, company_context: str = "") -> Goal:
        logger.info(f"Interpreting goal for task: {task[:80]}...")

        user_content = f"Task: {task}\n"
        if company_context:
            user_content += f"\nCompany Context:\n{company_context}\n"

        messages = [
            LLMMessage(role="system", content=INTERPRETER_SYSTEM_PROMPT),
            LLMMessage(role="user", content=user_content),
        ]

        try:
            response = await self.llm.generate_async(
                messages=messages,
                response_model=Goal,
                temperature=0.1,
            )
            goal = response.get_parsed(Goal)
            logger.info(f"Interpreted outcome: {goal.outcome} (is_blocking={goal.is_blocking})")
            return goal
        except Exception as e:
            logger.warning(f"Structured goal interpretation failed, falling back to heuristics: {e}")
            # Resilient fallback
            return Goal(
                outcome=task,
                success_criteria=[f"Task completed: {task}"],
                constraints=["Follow company policies and procedures"],
                unknowns=[],
                is_blocking=False,
            )
