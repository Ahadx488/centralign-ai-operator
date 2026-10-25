from __future__ import annotations

import json
from typing import Any, TypeVar

from pydantic import BaseModel, Field

T = TypeVar("T", bound=BaseModel)


class TokenUsage(BaseModel):
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class ToolDefinition(BaseModel):
    name: str
    description: str
    parameters: dict[str, Any]


class LLMMessage(BaseModel):
    role: str  # "system", "user", "assistant", "tool"
    content: str
    tool_calls: list[ToolCall] | None = None
    tool_call_id: str | None = None
    name: str | None = None


class LLMResponse(BaseModel):
    content: str | None = None
    parsed: Any | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)
    usage: TokenUsage = Field(default_factory=TokenUsage)
    latency_ms: float = 0.0
    model: str = ""
    provider: str = ""
    raw: Any | None = None

    def get_parsed(self, model_cls: type[T]) -> T:
        """Parse raw content or parsed dict into a target Pydantic model."""
        if self.parsed is not None and isinstance(self.parsed, model_cls):
            return self.parsed
        if self.parsed is not None and isinstance(self.parsed, dict):
            return model_cls.model_validate(self.parsed)
        if self.content:
            clean = self.content.strip()
            if clean.startswith("```json"):
                clean = clean[7:]
            elif clean.startswith("```"):
                clean = clean[3:]
            clean = clean.removesuffix("```").strip()
            try:
                data = json.loads(clean)
            except json.JSONDecodeError as err:
                raise ValueError(f"Content is not valid JSON: {err}") from err
            try:
                return model_cls.model_validate(data)
            except Exception as err:
                raise ValueError(
                    f"Failed to validate JSON against {model_cls.__name__}: {err}"
                ) from err
        raise ValueError("No content or parsed object available to validate against model")
