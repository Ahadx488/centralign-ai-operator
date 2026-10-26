from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml

logger = logging.getLogger(__name__)


class CredentialVault:
    """
    Secure Credential Vault.
    Resolves credential references (e.g. 'vault:erp_credentials') from company/vault.yaml
    or system environment variables.
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


# Global default instance
credential_vault = CredentialVault()
