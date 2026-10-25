from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlparse

import httpx
from pydantic import BaseModel, Field

from core.tools.registry import BaseTool, RiskTier, ToolResult

logger = logging.getLogger(__name__)

# Allowed hosts for mock environment API requests
ALLOWED_HOSTS = ["localhost:8001", "127.0.0.1:8001", "localhost:8000", "127.0.0.1:8000"]


class HttpRequestArgs(BaseModel):
    method: str = Field(default="GET", description="HTTP method: GET, POST, PUT, DELETE")
    url: str = Field(description="Target URL to request (restricted to mock environment endpoints)")
    headers: dict[str, str] | None = Field(default=None, description="Optional request HTTP headers")
    json_data: Any | None = Field(default=None, description="Optional JSON request payload")
    form_data: dict[str, str] | None = Field(default=None, description="Optional form-encoded request data")


class HttpRequestTool(BaseTool):
    name = "http_request"
    description = (
        "Send an HTTP request to an allowed internal API endpoint (restricted to mock environment hosts)."
    )
    args_schema = HttpRequestArgs
    risk_tier = RiskTier.REVERSIBLE_WRITE

    async def _run(self, **kwargs: Any) -> ToolResult:
        method: str = kwargs.get("method", "GET")
        url: str = kwargs["url"]
        headers: dict[str, str] | None = kwargs.get("headers")
        json_data: Any | None = kwargs.get("json_data")
        form_data: dict[str, str] | None = kwargs.get("form_data")

        parsed = urlparse(url)
        netloc = parsed.netloc.lower()

        # Host allowlist validation
        if not any(
            netloc == allowed or netloc.startswith("localhost") or netloc.startswith("127.0.0.1")
            for allowed in ALLOWED_HOSTS
        ):
            return ToolResult.failure(
                f"Access denied: Host '{netloc}' is not in allowed hosts: {ALLOWED_HOSTS}"
            )

        # STRICT GUARDRAIL: Prohibit agent from accessing /__truth endpoints
        if "/__truth" in parsed.path:
            logger.warning(
                f"Security block: Agent attempted unauthorized call to ground-truth endpoint '{url}'"
            )
            return ToolResult.failure(
                "Access denied: Access to ground truth endpoints (/__truth) is strictly prohibited."
            )

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                res = await client.request(
                    method=method.upper(),
                    url=url,
                    headers=headers,
                    json=json_data,
                    data=form_data,
                )

            is_json = False
            json_body = None
            try:
                json_body = res.json()
                is_json = True
            except Exception:
                pass

            return ToolResult.success(
                data={
                    "status_code": res.status_code,
                    "url": str(res.url),
                    "is_json": is_json,
                    "json": json_body,
                    "text": res.text if not is_json else None,
                },
                evidence_refs=[url],
            )
        except Exception as e:
            return ToolResult.failure(f"HTTP request to '{url}' failed: {e}")
