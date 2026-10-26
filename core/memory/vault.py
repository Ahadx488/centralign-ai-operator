from __future__ import annotations

import logging
import os
import re
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class CredentialVault:
    """
    Secure Credential Vault.
    Resolves credential references (e.g. 'vault:erp_credentials.password') from company/vault.yaml
    or system environment variables, while ensuring raw secrets are NEVER exposed to LLM prompts,
    logs, memory, or step records.
    """

    def __init__(self, vault_path: str | Path = "company/vault.yaml") -> None:
        self.vault_path = Path(vault_path)
        self._credentials: dict[str, Any] = {}
        self.reload()

    def reload(self) -> None:
        """Reload credentials from vault file on disk."""
        if self.vault_path.exists():
            try:
                data = yaml.safe_load(self.vault_path.read_text(encoding="utf-8")) or {}
                if isinstance(data, dict):
                    self._credentials = data
                    logger.debug(f"Loaded {len(self._credentials)} credential sets from {self.vault_path}")
            except Exception as e:
                logger.error(f"Failed to load credentials from {self.vault_path}: {e}")
        else:
            logger.warning(f"Vault file '{self.vault_path}' not found.")

    def get(self, vault_ref: str) -> dict[str, Any]:
        """
        Resolve a vault reference (e.g. 'vault:erp_credentials' or 'erp_credentials')
        into a dictionary of credential attributes.
        """
        clean_key = vault_ref.removeprefix("vault:").strip()
        if clean_key in self._credentials:
            return dict(self._credentials[clean_key])

        # Check environment variables fallback
        env_user = os.getenv(f"{clean_key.upper()}_USERNAME")
        env_pass = os.getenv(f"{clean_key.upper()}_PASSWORD")
        if env_user and env_pass:
            return {"username": env_user, "password": env_pass}

        return {}

    def get_system_credentials(
        self,
        system_key: str,
        systems_config: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Resolve credentials for a given system key (e.g. 'erp', 'vendor_portal', 'ticketing')."""
        if systems_config and "systems" in systems_config:
            sys_info = systems_config["systems"].get(system_key, {})
            login_info = sys_info.get("login", {})
            vault_ref = login_info.get("default_credentials")
            if vault_ref:
                return self.get(vault_ref)

        # Default key resolution
        return self.get(f"{system_key}_credentials")

    def resolve_secret(self, ref: str) -> str:
        """
        Resolve a secret reference like 'erp.password' or 'erp_credentials.password'
        or 'vault:erp_credentials.password' into the plaintext secret value.
        """
        clean = ref.removeprefix("vault:").strip()
        if "." in clean:
            target, field = clean.split(".", 1)
        else:
            target, field = clean, "password"

        # Check aliases: e.g. erp -> erp_credentials, portal -> vendor_portal_credentials
        candidates = [target]
        if not target.endswith("_credentials"):
            candidates.append(f"{target}_credentials")
        if target == "portal":
            candidates.extend(["vendor_portal", "vendor_portal_credentials"])
        if target == "vendor_portal":
            candidates.append("vendor_portal_credentials")
        if target == "tickets":
            candidates.append("tickets_credentials")

        for cand in candidates:
            if cand in self._credentials:
                val = self._credentials[cand]
                if isinstance(val, dict) and field in val:
                    return str(val[field])
                if isinstance(val, str):
                    return val

        # Check environment variables
        env_key = f"{target.upper()}_{field.upper()}"
        if env_key in os.environ:
            return os.environ[env_key]

        return ""

    def resolve_templates(self, data: Any) -> Any:
        """
        Recursively traverse dictionaries, lists, and strings, resolving any
        '{{vault:<ref>}}' references to actual secret values immediately before tool execution.
        """
        if isinstance(data, str):
            def repl(match: re.Match[str]) -> str:
                ref = match.group(1).strip()
                return self.resolve_secret(ref)

            return re.sub(r"\{\{vault:([^\}]+)\}\}", repl, data)
        elif isinstance(data, dict):
            return {k: self.resolve_templates(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.resolve_templates(item) for item in data]
        return data

    def get_all_secrets(self) -> set[str]:
        """
        Collect all raw secret values stored in the vault or sensitive env vars.
        Excludes non-secrets or very short strings to avoid over-redaction.
        """
        secrets: set[str] = set()
        for cred_set in self._credentials.values():
            if isinstance(cred_set, dict):
                for k, v in cred_set.items():
                    if k in {"password", "secret", "token", "api_key", "key"} and isinstance(v, str) and len(v) >= 4:
                        secrets.add(v)
            elif isinstance(cred_set, str) and len(cred_set) >= 4:
                secrets.add(cred_set)

        gemini_key = os.getenv("GEMINI_API_KEY")
        if gemini_key and len(gemini_key) >= 6:
            secrets.add(gemini_key)

        return secrets

    def sanitize_secrets(self, data: Any) -> Any:
        """
        Recursively redact known secrets from strings, dictionaries, lists, or objects.
        Replaces raw secret values with '[REDACTED_SECRET]'.
        """
        secrets = self.get_all_secrets()
        if not secrets:
            return data

        if isinstance(data, str):
            res = data
            for sec in secrets:
                if sec in res:
                    res = res.replace(sec, "[REDACTED_SECRET]")
            return res
        elif isinstance(data, dict):
            return {k: self.sanitize_secrets(v) for k, v in data.items()}
        elif isinstance(data, list):
            return [self.sanitize_secrets(item) for item in data]
        return data


# Global default instance
credential_vault = CredentialVault()
