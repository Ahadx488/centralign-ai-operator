from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel, Field

logger = logging.getLogger(__name__)


class ConnectorAuth(BaseModel):
    type: str = "none"  # "form", "token", "none"
    login_url: str | None = None
    username_field: str | None = None
    password_field: str | None = None
    default_credentials: str | None = None


class ConnectorEndpoint(BaseModel):
    name: str
    path: str
    method: str = "GET"
    description: str = ""
    fields: list[str] = Field(default_factory=list)


class ConnectorVerificationHint(BaseModel):
    target_table: str | None = None
    query_sql: str | None = None
    expected_conditions: list[str] = Field(default_factory=list)


class ConnectorSpec(BaseModel):
    """Declarative specification for external and internal company systems."""

    name: str
    system_key: str
    description: str
    base_url: str
    auth: ConnectorAuth | None = None
    tool_bindings: list[str] = Field(default_factory=list)
    endpoints: list[ConnectorEndpoint] = Field(default_factory=list)
    dom_hints: dict[str, str] = Field(default_factory=dict)
    verification_hints: list[ConnectorVerificationHint] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class ConnectorRegistry:
    """Manages discovery and loading of declarative connector specifications."""

    def __init__(self, connectors_dir: Path | str = "company/connectors") -> None:
        self.connectors_dir = Path(connectors_dir)
        self._connectors: dict[str, ConnectorSpec] = {}
        self.reload()

    def reload(self) -> None:
        """Scan directory and parse all connector YAML files."""
        self._connectors.clear()
        if not self.connectors_dir.exists():
            logger.info(f"Connectors dir '{self.connectors_dir}' does not exist yet.")
            return

        for p in self.connectors_dir.glob("*.yaml"):
            try:
                data = yaml.safe_load(p.read_text(encoding="utf-8")) or {}
                spec = ConnectorSpec.model_validate(data)
                self._connectors[spec.system_key] = spec
            except Exception as e:
                logger.error(f"Failed to load connector from {p}: {e}")

        logger.info(f"Loaded {len(self._connectors)} declarative connectors from {self.connectors_dir}")

    def get(self, system_key: str) -> ConnectorSpec | None:
        return self._connectors.get(system_key)

    def list_connectors(self) -> list[ConnectorSpec]:
        return list(self._connectors.values())


connector_registry = ConnectorRegistry()
