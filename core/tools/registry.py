from __future__ import annotations

import abc
import logging
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, ValidationError

from core.llm.models import ToolDefinition

logger = logging.getLogger(__name__)


class RiskTier(str, Enum):
    """
    Action classification for policy enforcement and human-in-the-loop approvals.
    """

    READ = "READ"  # Read-only, inspection, query
    REVERSIBLE_WRITE = "REVERSIBLE_WRITE"  # Create draft, reversible local write
    IRREVERSIBLE_WRITE = "IRREVERSIBLE_WRITE"  # Financial post, delete, submit final bill
    EXTERNAL = "EXTERNAL"  # Send email, call third-party endpoint


class ToolResult(BaseModel):
    """
    Uniform envelope returned by every tool execution.
    Never raises unhandled exceptions to callers.
    """

    ok: bool
    data: Any = None
    error: str | None = None
    evidence_refs: list[str] = Field(default_factory=list)

    @classmethod
    def success(cls, data: Any = None, evidence_refs: list[str] | None = None) -> ToolResult:
        return cls(ok=True, data=data, evidence_refs=evidence_refs or [])

    @classmethod
    def failure(cls, error: str, evidence_refs: list[str] | None = None) -> ToolResult:
        return cls(ok=False, error=error, evidence_refs=evidence_refs or [])


class BaseTool(abc.ABC):
    """
    Abstract base class for all CentrAlign AI tools.
    Encapsulates schema, risk tier, execution safety, and verifier hints.
    """

    name: str
    description: str
    args_schema: type[BaseModel]
    risk_tier: RiskTier = RiskTier.READ

    async def execute(self, **kwargs: Any) -> ToolResult:
        """
        Public execution wrapper ensuring validation and uniform error handling.
        """
        try:
            # 1. Validate arguments against schema
            validated_args = self.args_schema.model_validate(kwargs)
            # 2. Run tool logic
            result = await self._run(**validated_args.model_dump())
            return result
        except ValidationError as ve:
            logger.warning(f"Validation error in tool '{self.name}': {ve}")
            return ToolResult.failure(f"Invalid arguments for tool '{self.name}': {ve}")
        except Exception as e:
            logger.error(f"Execution error in tool '{self.name}': {e}", exc_info=True)
            return ToolResult.failure(f"Tool '{self.name}' execution failed: {e}")

    @abc.abstractmethod
    async def _run(self, **kwargs: Any) -> ToolResult:
        """Concrete tool implementation to be provided by subclass."""

    def verify_hint(self, result: ToolResult) -> str | None:
        """Optional hint indicating how the verifier can confirm this action's state change."""
        return None

    def to_tool_definition(self) -> ToolDefinition:
        """Export tool metadata to LLM tool definition format."""
        schema = self.args_schema.model_json_schema()
        # Clean title for LLM readability
        schema.pop("title", None)
        return ToolDefinition(
            name=self.name,
            description=self.description,
            parameters=schema,
        )


class ToolRegistry:
    """
    Central repository of registered tools available to the agent.
    """

    def __init__(self) -> None:
        self._tools: dict[str, BaseTool] = {}

    def register(self, tool: BaseTool) -> None:
        self._tools[tool.name] = tool
        logger.debug(f"Registered tool: {tool.name} (Risk: {tool.risk_tier.value})")

    def unregister(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> BaseTool | None:
        if name in self._tools:
            return self._tools[name]
        # Resolve common namespace aliases
        for prefix in ("browser_", "files_", "mail_"):
            candidate = f"{prefix}{name}"
            if candidate in self._tools:
                return self._tools[candidate]
        for prefix in ("browser_", "files_", "mail_"):
            if name.startswith(prefix):
                stripped = name[len(prefix):]
                if stripped in self._tools:
                    return self._tools[stripped]
        return None

    def list_tools(self) -> list[BaseTool]:
        return list(self._tools.values())

    def export_definitions(self) -> list[ToolDefinition]:
        return [tool.to_tool_definition() for tool in self._tools.values()]

    async def execute(self, name: str, args: dict[str, Any]) -> ToolResult:
        tool = self.get(name)
        if not tool:
            return ToolResult.failure(
                f"Tool '{name}' is not registered. Available tools: {list(self._tools.keys())}"
            )
        return await tool.execute(**args)


# Global singleton tool registry
tool_registry = ToolRegistry()
