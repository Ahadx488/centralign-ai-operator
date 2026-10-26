from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import yaml

from core.policy.models import PolicyDecision, RunCapabilities
from core.tools.registry import RiskTier

logger = logging.getLogger(__name__)


class PolicyEngine:
    """
    CentrAlign Enterprise Policy Engine.
    Evaluates every tool action before execution against dynamic rules in company/policies.yaml,
    tool risk tiers, and run capability boundaries.
    """

    def __init__(self, policy_file: Path | str = "company/policies.yaml") -> None:
        self.policy_file = Path(policy_file)
        self._last_mtime: float = 0.0
        self._rules: list[dict[str, Any]] = []
        self._cached_config: dict[str, Any] = {}
        self.reload_if_changed()

    def reload_if_changed(self) -> None:
        """Check if policy file was modified on disk and reload dynamically."""
        if not self.policy_file.exists():
            logger.warning(f"Policy file '{self.policy_file}' not found. Using default permissive policy.")
            self._rules = []
            return

        try:
            mtime = os.path.getmtime(self.policy_file)
            if mtime > self._last_mtime:
                raw = yaml.safe_load(self.policy_file.read_text(encoding="utf-8")) or {}
                self._cached_config = raw
                self._rules = raw.get("rules", [])
                self._last_mtime = mtime
                logger.info(f"Loaded {len(self._rules)} dynamic policy rules from {self.policy_file} (mtime={mtime})")
        except Exception as e:
            logger.error(f"Error loading policies from {self.policy_file}: {e}")

    def get_spending_threshold(self) -> float:
        """Dynamically extract current spending threshold from policies."""
        self.reload_if_changed()
        # 1. Check top-level parameter
        if "max_auto_approve_amount_inr" in self._cached_config:
            return float(self._cached_config["max_auto_approve_amount_inr"])

        # 2. Extract from rule condition
        for r in self._rules:
            if r.get("id") == "spending_threshold_approval":
                if "threshold" in r:
                    return float(r["threshold"])
                cond = str(r.get("condition", ""))
                match = re.search(r">\s*(\d+(?:\.\d+)?)", cond)
                if match:
                    return float(match.group(1))

        return 50000.0

    def evaluate(
        self,
        tool_name: str,
        tool_args: dict[str, Any],
        risk_tier: RiskTier | str = RiskTier.READ,
        capabilities: RunCapabilities | None = None,
        context: dict[str, Any] | None = None,
    ) -> PolicyDecision:
        """
        Evaluate tool execution against runtime capability boundaries and company policies.
        Returns PolicyDecision(effect=ALLOW | REQUIRE_APPROVAL | DENY).
        """
        self.reload_if_changed()
        context = context or {}
        tier_str = risk_tier.value if isinstance(risk_tier, RiskTier) else str(risk_tier)

        # -------------------------------------------------------------
        # 1. Runtime Capability Scoping Checks
        # -------------------------------------------------------------
        if capabilities is not None:
            # 1a. Tool allowlist
            if capabilities.allowed_tools is not None and tool_name not in capabilities.allowed_tools:
                return PolicyDecision.deny(
                    rule_id="runtime_tool_scope",
                    reason=f"Tool '{tool_name}' is not permitted in this run's capability scope.",
                    risk_tier=tier_str,
                )

            # 1b. Domain scoping for URLs
            target_url = tool_args.get("url")
            if target_url and isinstance(target_url, str):
                parsed = urlparse(target_url)
                host = parsed.netloc or parsed.path
                if not any(allowed in host or allowed in target_url for allowed in capabilities.allowed_domains):
                    return PolicyDecision.deny(
                        rule_id="runtime_domain_scope",
                        reason=f"Network navigation to '{target_url}' is outside permitted domains.",
                        risk_tier=tier_str,
                    )

            # 1c. File directory scoping
            file_path = tool_args.get("path") or tool_args.get("file_path")
            if file_path and isinstance(file_path, str) and tool_name.startswith("files_"):
                norm_p = os.path.normpath(file_path)
                is_allowed = any(norm_p.startswith(os.path.normpath(allowed)) for allowed in capabilities.allowed_file_folders)
                if not is_allowed:
                    return PolicyDecision.deny(
                        rule_id="runtime_file_scope",
                        reason=f"File operation on '{file_path}' is outside permitted directories.",
                        risk_tier=tier_str,
                    )

        # -------------------------------------------------------------
        # 2. Dynamic Rules from policies.yaml
        # -------------------------------------------------------------
        threshold = self.get_spending_threshold()

        # Rule: prohibit_record_deletion
        if (
            "delete" in tool_name.lower()
            or "purge" in tool_name.lower()
            or any(k in str(tool_args).lower() for k in ["delete from", "drop table", "rm -rf"])
        ):
            return PolicyDecision.deny(
                rule_id="prohibit_record_deletion",
                reason="Agents are strictly forbidden from deleting invoices, bills, or audit records.",
                risk_tier=tier_str,
            )

        # Rule: file_access_restriction
        if tool_name in {"read_file", "files_read_file"}:
            path = str(tool_args.get("path") or tool_args.get("file_path") or "")
            norm = os.path.normpath(path)
            # Permitted if within fileshare data, artifacts/downloads, or temporary directories
            is_permitted = (
                any(k in norm for k in ["env/fileshare/data", "artifacts", "downloads", "tmp", "pytest"])
                or (not os.path.isabs(norm) and not norm.startswith(".."))
            ) and not (norm.startswith("/etc") or norm.startswith("/var/root") or "/.ssh" in norm)

            if not is_permitted:
                return PolicyDecision.deny(
                    rule_id="file_access_restriction",
                    reason=f"File operations are strictly confined to the fileshare directory (attempted: {path}).",
                    risk_tier=tier_str,
                )

        # Rule: generic egress communication policy from company/policies.yaml
        if tool_name in {"mail_send", "send_email", "mail_send_email"}:
            recipient = str(tool_args.get("recipient") or tool_args.get("to") or "").strip().lower()
            recip_domain = recipient.split("@")[-1] if "@" in recipient else ""

            egress_cfg = self._cached_config.get("egress", {})
            allowed_domains = [d.lower() for d in egress_cfg.get("allowed_domains", ["company.com"])]
            denied_domains = [d.lower() for d in egress_cfg.get("denied_domains", [])]
            default_action = egress_cfg.get("default_action", "REQUIRE_APPROVAL").upper()

            # 1. Denied domain check
            if any(recip_domain == d or recip_domain.endswith(f".{d}") for d in denied_domains):
                return PolicyDecision.deny(
                    rule_id="prompt_injection_exfiltration_blocked",
                    reason=f"SECURITY ALERT: Blocked outbound communication to denied domain '{recip_domain}'.",
                    risk_tier=tier_str,
                )

            # 2. Allowed domain check
            if any(recip_domain == d or recip_domain.endswith(f".{d}") for d in allowed_domains):
                return PolicyDecision.allow(
                    reason=f"Internal company email communication permitted to '{recip_domain}'",
                    risk_tier=tier_str,
                )

            # 3. External unlisted domain -> require approval or deny
            if default_action == "DENY":
                return PolicyDecision.deny(
                    rule_id="egress_unlisted_domain_denied",
                    reason=f"Sending email to external domain '{recip_domain}' is denied by company egress policy.",
                    risk_tier=tier_str,
                )
            else:
                return PolicyDecision.require_approval(
                    rule_id="external_communication_approval",
                    reason=f"Sending email to external recipient '{recipient}' requires human manager approval.",
                    approver_role="finance_manager",
                    risk_tier=tier_str,
                    metadata={"recipient": recipient, "domain": recip_domain},
                )

        # Rule: spending_threshold_approval
        # Check if action enters/submits a bill with amount > threshold
        bill_amount: float | None = None
        if "amount" in tool_args:
            try:
                bill_amount = float(tool_args["amount"])
            except (ValueError, TypeError):
                pass
        elif tool_name == "browser_type_text" and any(k in str(tool_args.get("target", "")).lower() for k in ["amount", "#amount", "bill-amount"]):
            try:
                bill_amount = float(str(tool_args.get("text", "")).replace(",", "").strip())
            except (ValueError, TypeError):
                pass
        elif tool_name == "browser_click" and any(k in str(tool_args.get("target", "")).lower() for k in ["submit bill", "process & post", "post bill"]):
            # Check context working memory for recorded amount
            if "amount" in context:
                try:
                    bill_amount = float(context["amount"])
                except (ValueError, TypeError):
                    pass

        if bill_amount is not None and bill_amount > threshold:
            return PolicyDecision.require_approval(
                rule_id="spending_threshold_approval",
                reason=f"Bill amount ₹{bill_amount:,.2f} exceeds auto-approval threshold of ₹{threshold:,.2f}.",
                approver_role="finance_manager",
                risk_tier=tier_str,
                metadata={"amount": bill_amount, "threshold": threshold},
            )

        # -------------------------------------------------------------
        # 3. Risk Tier Fallback Check
        # -------------------------------------------------------------
        if tier_str in {RiskTier.IRREVERSIBLE_WRITE.value, RiskTier.EXTERNAL.value}:
            return PolicyDecision.require_approval(
                rule_id="high_risk_tier",
                reason=f"Action '{tool_name}' has risk tier {tier_str} and requires authorization.",
                approver_role="operator",
                risk_tier=tier_str,
            )

        return PolicyDecision.allow(reason="Action permitted by policy engine", risk_tier=tier_str)
