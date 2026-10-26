from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, Field

from core.tools.registry import BaseTool, RiskTier, ToolResult

logger = logging.getLogger(__name__)


class AskUserArgs(BaseModel):
    question: str = Field(description="The specific blocking question to ask the user")
    options: list[str] | None = Field(
        default=None,
        description="Optional list of selectable choices for the user",
    )


class AskUserTool(BaseTool):
    name = "ask_user"
    description = (
        "Ask the human user a clarifying question when critical missing information cannot be found in memory or tools."
    )
    args_schema = AskUserArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        question: str = kwargs["question"]
        options: list[str] | None = kwargs.get("options")
        logger.info(f"Agent asking human user: '{question}' (Options: {options})")
        return ToolResult.success(
            data={
                "signal": "WAITING_USER",
                "question": question,
                "options": options or [],
            }
        )


class RequestApprovalArgs(BaseModel):
    action: str = Field(description="The sensitive or high-risk action to be executed")
    reason: str = Field(description="The justification or policy trigger requiring approval")
    risk_tier: str = Field(
        default="IRREVERSIBLE_WRITE",
        description="Risk tier: IRREVERSIBLE_WRITE, EXTERNAL, etc.",
    )


class RequestApprovalTool(BaseTool):
    name = "request_approval"
    description = (
        "Pause the run and request explicit human manager approval before executing high-risk or threshold-exceeding actions."
    )
    args_schema = RequestApprovalArgs
    risk_tier = RiskTier.READ

    async def _run(self, **kwargs: Any) -> ToolResult:
        action: str = kwargs["action"]
        reason: str = kwargs["reason"]
        risk_tier: str = kwargs.get("risk_tier", "IRREVERSIBLE_WRITE")
        logger.info(f"Agent requesting approval for action: '{action}' | Reason: {reason}")
        return ToolResult.success(
            data={
                "signal": "WAITING_APPROVAL",
                "action": action,
                "reason": reason,
                "risk_tier": risk_tier,
            }
        )
