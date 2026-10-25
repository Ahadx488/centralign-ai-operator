from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class Goal(BaseModel):
    """Structured interpretation of the user task."""
    outcome: str = Field(description="Clear, concise definition of the desired end state.")
    success_criteria: list[str] = Field(
        default_factory=list,
        description="List of verifiable conditions that must be true for task completion.",
    )
    constraints: list[str] = Field(
        default_factory=list,
        description="Operating boundaries, compliance rules, or limitations.",
    )
    unknowns: list[str] = Field(
        default_factory=list,
        description="Missing facts or ambiguities in the request.",
    )
    is_blocking: bool = Field(
        default=False,
        description="True if missing information prevents taking ANY autonomous action without user input.",
    )
    clarification_question: str | None = Field(
        default=None,
        description="Question to present to the user if is_blocking is True.",
    )


class IntentStatus(str, Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class Intent(BaseModel):
    """High-level milestone intent in a living plan."""
    id: str
    description: str
    expected_outcome: str
    status: IntentStatus = IntentStatus.PENDING
    retries: int = 0
    max_retries: int = 3
    notes: str | None = None


class Plan(BaseModel):
    """A living plan composed of dynamic intents."""
    intents: list[Intent] = Field(default_factory=list)
    current_intent_index: int = 0
    replan_count: int = 0

    @property
    def current_intent(self) -> Intent | None:
        if 0 <= self.current_intent_index < len(self.intents):
            return self.intents[self.current_intent_index]
        return None

    @property
    def is_completed(self) -> bool:
        if not self.intents:
            return False
        return all(i.status in {IntentStatus.COMPLETED, IntentStatus.SKIPPED} for i in self.intents)

    def advance(self) -> Intent | None:
        if self.current_intent:
            self.current_intent.status = IntentStatus.COMPLETED
        self.current_intent_index += 1
        return self.current_intent


class ObservationType(str, Enum):
    PROGRESS = "progress"
    NO_OP = "no_op"
    ERROR = "error"
    SURPRISE = "surprise"


class Reflection(BaseModel):
    """Post-action reflection distilling observation into understanding."""
    observation_type: ObservationType = ObservationType.PROGRESS
    summary: str
    intent_completed: bool = False
    facts_learned: dict[str, Any] = Field(default_factory=dict)
    suggested_next_action: str | None = None
    needs_replan: bool = False


class AgentAction(BaseModel):
    """Action decision produced by the executor LLM."""
    thought: str = Field(description="Chain-of-thought rationale for this step.")
    tool: str | None = Field(default=None, description="Name of tool to execute, or None if finishing.")
    args: dict[str, Any] = Field(default_factory=dict, description="Keyword arguments for the tool.")
    is_finish: bool = Field(default=False, description="True if the goal is fully accomplished.")
    finish_summary: str | None = Field(default=None, description="Final summary of task outcome.")
