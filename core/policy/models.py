from __future__ import annotations

from enum import Enum
from typing import Any

from pydantic import BaseModel, Field


class PolicyEffect(str, Enum):
    ALLOW = "ALLOW"
    REQUIRE_APPROVAL = "REQUIRE_APPROVAL"
    DENY = "DENY"


class PolicyDecision(BaseModel):
    """The outcome of a policy engine evaluation before tool execution."""

    effect: PolicyEffect
    rule_id: str | None = None
    reason: str = ""
    approver_role: str | None = None
    risk_tier: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)

    @classmethod
    def allow(cls, reason: str = "Permitted by policy", risk_tier: str | None = None) -> PolicyDecision:
        return cls(effect=PolicyEffect.ALLOW, reason=reason, risk_tier=risk_tier)

    @classmethod
    def require_approval(
        cls,
        rule_id: str,
        reason: str,
        approver_role: str = "manager",
        risk_tier: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        return cls(
            effect=PolicyEffect.REQUIRE_APPROVAL,
            rule_id=rule_id,
            reason=reason,
            approver_role=approver_role,
            risk_tier=risk_tier,
            metadata=metadata or {},
        )

    @classmethod
    def deny(
        cls,
        rule_id: str,
        reason: str,
        risk_tier: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        return cls(
            effect=PolicyEffect.DENY,
            rule_id=rule_id,
            reason=reason,
            risk_tier=risk_tier,
            metadata=metadata or {},
        )


class RunCapabilities(BaseModel):
    """Runtime capability boundary enforced strictly per run in Python runtime."""

    allowed_tools: list[str] | None = None  # None = all registered tools allowed
    allowed_domains: list[str] = Field(
        default_factory=lambda: ["localhost:8001", "127.0.0.1:8001", "http://localhost:8001", "http://127.0.0.1:8001"]
    )
    allowed_file_folders: list[str] = Field(
        default_factory=lambda: ["./env/fileshare/data", "./artifacts", "artifacts", "env/fileshare/data"]
    )
    max_amount_threshold: float | None = None
