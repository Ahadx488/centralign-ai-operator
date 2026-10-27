from __future__ import annotations

import abc
import asyncio
import json
import logging
import os
import time
from typing import Any, TypeVar

import httpx
from dotenv import load_dotenv
from pydantic import BaseModel

load_dotenv()

from core.llm.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolDefinition  # noqa: E402

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Base exception for LLM errors."""


class LLMRetryableError(LLMError):
    """Error that is safe to retry (e.g. rate limit, 503, transient network)."""

    def __init__(self, message: str, retry_after: float | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


def pydantic_to_gemini_schema(
    schema_or_model: type[BaseModel] | dict[str, Any],
) -> dict[str, Any]:
    """
    Convert a Pydantic model or standard JSON schema into a Gemini OpenAPI 3.0 compatible Schema.
    - Resolves all $defs / $ref inlines
    - Maps type names to uppercase: OBJECT, STRING, INTEGER, NUMBER, BOOLEAN, ARRAY
    - Strips unsupported keys ($schema, $defs, definitions, title, additionalProperties)
    - Recursively formats nested properties and array items
    """
    if isinstance(schema_or_model, type) and issubclass(schema_or_model, BaseModel):
        raw_schema = schema_or_model.model_json_schema()
    else:
        raw_schema = dict(schema_or_model)

    defs: dict[str, Any] = raw_schema.pop("$defs", {}) or raw_schema.pop("definitions", {})

    def resolve_ref(ref_str: str) -> dict[str, Any]:
        name = ref_str.split("/")[-1]
        if name in defs:
            return dict(defs[name])
        return {}

    def transform_node(node: dict[str, Any]) -> dict[str, Any]:
        if "$ref" in node:
            ref_schema = resolve_ref(node["$ref"])
            merged = {**ref_schema, **{k: v for k, v in node.items() if k != "$ref"}}
            return transform_node(merged)

        out: dict[str, Any] = {}

        # Handle type
        raw_type = node.get("type")
        if raw_type:
            if isinstance(raw_type, list):
                types = [t for t in raw_type if t != "null"]
                if types:
                    out["type"] = types[0].upper()
                if "null" in raw_type:
                    out["nullable"] = True
            elif isinstance(raw_type, str):
                out["type"] = raw_type.upper()

        # Handle anyOf (commonly used for Optional / Union with None)
        if "anyOf" in node:
            non_null = [opt for opt in node["anyOf"] if opt.get("type") != "null"]
            if non_null:
                transformed = transform_node(non_null[0])
                out.update(transformed)
                if len(non_null) < len(node["anyOf"]):
                    out["nullable"] = True

        if "description" in node:
            out["description"] = node["description"]
        if "enum" in node:
            out["enum"] = node["enum"]
        if "format" in node:
            out["format"] = node["format"]
        if "required" in node:
            out["required"] = node["required"]
        if "nullable" in node:
            out["nullable"] = node["nullable"]

        # Handle properties (for OBJECT)
        if "properties" in node:
            out["properties"] = {}
            for prop_name, prop_val in node["properties"].items():
                if isinstance(prop_val, dict):
                    transformed = transform_node(prop_val)
                    if prop_name == "tool":
                        transformed["enum"] = [
                            "browser_open_url",
                            "browser_snapshot",
                            "browser_click",
                            "browser_type_text",
                            "browser_select_option",
                            "browser_press_key",
                            "browser_wait_for",
                            "browser_screenshot",
                            "browser_extract_text",
                            "browser_download_file",
                            "browser_upload_file",
                            "browser_get_tabs",
                            "files_list_dir",
                            "files_read_file",
                            "files_write_file",
                            "files_search_files",
                            "http_request",
                            "mail_search",
                            "mail_read",
                            "mail_save_attachment",
                            "mail_send",
                            "ask_user",
                            "request_approval",
                        ]
                    out["properties"][prop_name] = transformed
        elif out.get("type") == "OBJECT":
            # For open-ended dictionaries (e.g. tool args dict[str, Any]), populate tool parameters
            out["properties"] = {
                "url": {"type": "STRING", "description": "Target URL to open or request"},
                "target": {"type": "STRING", "description": "Element selector, ref ID, or visible text"},
                "text": {"type": "STRING", "description": "Text to type into input field"},
                "clear": {"type": "BOOLEAN", "description": "Whether to clear existing text before typing"},
                "value": {"type": "STRING", "description": "Option value to select"},
                "key": {"type": "STRING", "description": "Keyboard key to press (e.g. Enter, Tab)"},
                "timeout_ms": {"type": "INTEGER", "description": "Timeout in milliseconds"},
                "name": {"type": "STRING", "description": "Label or name"},
                "path": {"type": "STRING", "description": "File or directory path"},
                "file_path": {"type": "STRING", "description": "File path to read, write, or upload"},
                "save_dir": {"type": "STRING", "description": "Directory to save downloaded file"},
                "pattern": {"type": "STRING", "description": "Glob search pattern (e.g. *.pdf)"},
                "base_dir": {"type": "STRING", "description": "Base directory for file search"},
                "method": {"type": "STRING", "description": "HTTP method: GET, POST, PUT, DELETE"},
                "headers": {"type": "OBJECT", "description": "Optional HTTP headers"},
                "json_data": {"type": "OBJECT", "description": "Optional JSON payload"},
                "form_data": {"type": "OBJECT", "description": "Optional form data"},
                "query": {"type": "STRING", "description": "Search query keyword"},
                "email_id": {"type": "INTEGER", "description": "ID of email to read or save attachment from"},
                "filename": {"type": "STRING", "description": "Filename of attachment or document"},
                "attachment_filename": {"type": "STRING", "description": "Filename of email attachment"},
                "recipient": {"type": "STRING", "description": "Email recipient"},
                "subject": {"type": "STRING", "description": "Email subject"},
                "body": {"type": "STRING", "description": "Email body content"},
                "question": {"type": "STRING", "description": "Question to ask user for clarification"},
                "options": {"type": "ARRAY", "items": {"type": "STRING"}, "description": "Clarification choices"},
                "action": {"type": "STRING", "description": "Action requiring approval"},
                "reason": {"type": "STRING", "description": "Reason approval is required"},
                "risk_tier": {"type": "STRING", "description": "Risk tier"},
                "vendor_name": {"type": "STRING", "description": "Vendor name"},
                "invoice_number": {"type": "STRING", "description": "Invoice number"},
                "amount": {"type": "STRING", "description": "Amount"},
                "due_date": {"type": "STRING", "description": "Payment due date YYYY-MM-DD"},
                "currency": {"type": "STRING", "description": "Currency code"},
            }

        # Handle items (for ARRAY)
        if "items" in node and isinstance(node["items"], dict):
            out["items"] = transform_node(node["items"])

        if "type" not in out and "properties" in out:
            out["type"] = "OBJECT"

        if "thought" in out.get("properties", {}) and "args" in out.get("properties", {}):
            out["required"] = ["thought", "tool", "args", "is_finish"]

        return out

    return transform_node(raw_schema)


class LLMClient(abc.ABC):
    """
    Provider-agnostic LLM Client interface.
    Supports structured output (Pydantic), tool calling, token/latency logging, and retries with backoff.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        max_retries: int = 6,
        initial_backoff_sec: float = 1.0,
        backoff_multiplier: float = 2.0,
        timeout_sec: float = 60.0,
    ):
        self.model = model
        self.api_key = api_key
        self.max_retries = max_retries
        self.initial_backoff_sec = initial_backoff_sec
        self.backoff_multiplier = backoff_multiplier
        self.timeout_sec = timeout_sec

    def generate(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """
        Execute call with automatic retry and backoff on transient errors.
        Logs latency and token usage on every invocation.
        """
        last_error: Exception | None = None
        backoff = self.initial_backoff_sec

        for attempt in range(1, self.max_retries + 1):
            start_time = time.time()
            try:
                response = self._execute_generate(
                    messages=messages,
                    tools=tools,
                    response_model=response_model,
                    temperature=temperature,
                )
                response.latency_ms = (time.time() - start_time) * 1000.0
                response.model = self.model

                if response_model and response.content and not response.parsed:
                    # Attempt to parse json content into response_model
                    try:
                        clean_content = response.content.strip()
                        if clean_content.startswith("```json"):
                            clean_content = clean_content[7:]
                        elif clean_content.startswith("```"):
                            clean_content = clean_content[3:]
                        clean_content = clean_content.removesuffix("```")
                        data = json.loads(clean_content.strip())
                        response.parsed = response_model.model_validate(data)
                    except Exception as e:
                        logger.warning(
                            f"Failed to parse structured output into {response_model.__name__}: {e}"
                        )

                # Token and latency logging as required by Phase 0
                logger.info(
                    "LLM call completed | provider=%s model=%s latency=%.2fms "
                    "prompt_tokens=%d completion_tokens=%d total_tokens=%d",
                    response.provider,
                    response.model,
                    response.latency_ms,
                    response.usage.prompt_tokens,
                    response.usage.completion_tokens,
                    response.usage.total_tokens,
                )

                return response

            except LLMRetryableError as e:
                last_error = e
                sleep_time = e.retry_after if e.retry_after is not None else backoff
                if sleep_time > 120:
                    logger.error(f"Rate limit retry delay ({sleep_time}s) exceeds max wait (120s). Failing fast: {e}")
                    raise LLMError(f"Rate limit quota exceeded: {e}") from e
                logger.warning(
                    "Attempt %d/%d failed with retryable error: %s. Retrying in %.3fs...",
                    attempt,
                    self.max_retries,
                    e,
                    sleep_time,
                )
                if attempt < self.max_retries:
                    time.sleep(sleep_time)
                    backoff *= self.backoff_multiplier
            except Exception as e:
                logger.error(f"LLM call failed with non-retryable error: {e}")
                raise

        raise LLMError(f"Exceeded max retries ({self.max_retries}): {last_error}")

    async def generate_async(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Asynchronous execution of generate in a background thread."""
        return await asyncio.to_thread(
            self.generate,
            messages=messages,
            tools=tools,
            response_model=response_model,
            temperature=temperature,
        )

    @abc.abstractmethod
    def _execute_generate(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        """Subclass implementation of the provider-specific generation."""


class GeminiLLMClient(LLMClient):
    """
    Direct Gemini API client via httpx REST endpoint.
    Complies with Google Gemini API v1beta specification.
    """

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        **kwargs: Any,
    ):
        model = model or os.getenv("LLM_MODEL") or "gemini-3.5-flash-lite"
        api_key = api_key or os.getenv("GEMINI_API_KEY")
        super().__init__(model=model, api_key=api_key, **kwargs)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

    def build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        """Construct the Gemini REST payload according to v1beta API spec."""
        contents: list[dict[str, Any]] = []
        system_instruction: dict[str, Any] | None = None

        for msg in messages:
            if msg.role == "system":
                system_instruction = {"parts": [{"text": msg.content}]}
            elif msg.role == "user":
                contents.append({"role": "user", "parts": [{"text": msg.content}]})
            elif msg.role == "assistant":
                parts: list[dict[str, Any]] = []
                if msg.content:
                    parts.append({"text": msg.content})
                if msg.tool_calls:
                    for tc in msg.tool_calls:
                        parts.append(
                            {
                                "functionCall": {
                                    "name": tc.name,
                                    "args": tc.arguments,
                                }
                            }
                        )
                contents.append({"role": "model", "parts": parts})
            elif msg.role == "tool":
                contents.append(
                    {
                        "role": "function",
                        "parts": [
                            {
                                "functionResponse": {
                                    "name": msg.name or "tool_result",
                                    "response": {"output": msg.content},
                                }
                            }
                        ],
                    }
                )

        payload: dict[str, Any] = {
            "contents": contents,
            "generationConfig": {
                "temperature": temperature,
            },
        }

        if system_instruction:
            payload["systemInstruction"] = system_instruction

        if response_model is not None:
            payload["generationConfig"]["responseMimeType"] = "application/json"
            payload["generationConfig"]["responseSchema"] = pydantic_to_gemini_schema(
                response_model
            )

        if tools:
            function_declarations = []
            for t in tools:
                function_declarations.append(
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": pydantic_to_gemini_schema(t.parameters),
                    }
                )
            payload["tools"] = [{"functionDeclarations": function_declarations}]

        return payload

    def parse_gemini_response(
        self, data: dict[str, Any], response_model: type[T] | None = None
    ) -> LLMResponse:
        """Parse Gemini REST response payload into unified LLMResponse."""
        candidates = data.get("candidates", [])
        if not candidates:
            return LLMResponse(content="", provider="gemini")

        candidate = candidates[0]
        content_obj = candidate.get("content", {})
        parts = content_obj.get("parts", [])

        text_content = ""
        tool_calls: list[ToolCall] = []

        for p in parts:
            if "text" in p:
                text_content += p["text"]
            if "functionCall" in p:
                fc = p["functionCall"]
                tool_calls.append(
                    ToolCall(
                        id=f"call_{int(time.time() * 1000)}",
                        name=fc["name"],
                        arguments=fc.get("args", {}),
                    )
                )

        usage_meta = data.get("usageMetadata", {})
        usage = TokenUsage(
            prompt_tokens=usage_meta.get("promptTokenCount", 0),
            completion_tokens=usage_meta.get("candidatesTokenCount", 0),
            total_tokens=usage_meta.get("totalTokenCount", 0),
        )

        parsed_obj = None
        if response_model and text_content:
            try:
                parsed_data = json.loads(text_content)
                parsed_obj = response_model.model_validate(parsed_data)
            except Exception as e:
                logger.warning(f"Error parsing Gemini JSON response: {e}")

        return LLMResponse(
            content=text_content,
            parsed=parsed_obj,
            tool_calls=tool_calls,
            usage=usage,
            provider="gemini",
            raw=data,
        )

    def _execute_generate(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("GEMINI_API_KEY is not set.")

        url = f"{self.base_url}/{self.model}:generateContent?key={self.api_key}"
        payload = self.build_payload(
            messages=messages,
            tools=tools,
            response_model=response_model,
            temperature=temperature,
        )

        try:
            with httpx.Client(timeout=self.timeout_sec) as client:
                res = client.post(url, json=payload)
        except httpx.RequestError as e:
            raise LLMRetryableError(f"Network error communicating with Gemini API: {e}") from e

        if res.status_code in (429, 500, 502, 503, 504):
            retry_after: float | None = None
            if res.status_code == 429:
                try:
                    err_json = res.json()
                    for detail in err_json.get("error", {}).get("details", []):
                        if "retryDelay" in detail:
                            delay_str = str(detail["retryDelay"])
                            retry_after = float(delay_str.rstrip("s")) + 1.0
                except Exception:
                    pass
                if retry_after is None:
                    retry_after = 20.0
            raise LLMRetryableError(
                f"Gemini API returned status {res.status_code}: {res.text}",
                retry_after=retry_after,
            )
        if res.status_code != 200:
            raise LLMError(f"Gemini API error ({res.status_code}): {res.text}")

        data = res.json()
        return self.parse_gemini_response(data, response_model=response_model)


class OpenAILLMClient(LLMClient):
    """
    OpenAI API client via httpx.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        **kwargs: Any,
    ):
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        super().__init__(model=model, api_key=api_key, **kwargs)
        self.base_url = base_url

    def build_payload(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> dict[str, Any]:
        openai_messages: list[dict[str, Any]] = []
        for msg in messages:
            m: dict[str, Any] = {"role": msg.role, "content": msg.content}
            if msg.tool_calls:
                m["tool_calls"] = [
                    {
                        "id": tc.id,
                        "type": "function",
                        "function": {
                            "name": tc.name,
                            "arguments": json.dumps(tc.arguments),
                        },
                    }
                    for tc in msg.tool_calls
                ]
            if msg.tool_call_id:
                m["tool_call_id"] = msg.tool_call_id
            if msg.name:
                m["name"] = msg.name
            openai_messages.append(m)

        payload: dict[str, Any] = {
            "model": self.model,
            "messages": openai_messages,
            "temperature": temperature,
        }

        if response_model is not None:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": response_model.__name__,
                    "strict": True,
                    "schema": response_model.model_json_schema(),
                },
            }

        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    },
                }
                for t in tools
            ]

        return payload

    def parse_openai_response(
        self, data: dict[str, Any], response_model: type[T] | None = None
    ) -> LLMResponse:
        choice = data["choices"][0]["message"]
        content = choice.get("content") or ""
        tool_calls: list[ToolCall] = []

        if "tool_calls" in choice and choice["tool_calls"]:
            for tc in choice["tool_calls"]:
                fn = tc.get("function", {})
                args: dict[str, Any] = {}
                try:
                    args = json.loads(fn.get("arguments", "{}"))
                except Exception:
                    pass
                tool_calls.append(ToolCall(id=tc["id"], name=fn.get("name", ""), arguments=args))

        usage_data = data.get("usage", {})
        usage = TokenUsage(
            prompt_tokens=usage_data.get("prompt_tokens", 0),
            completion_tokens=usage_data.get("completion_tokens", 0),
            total_tokens=usage_data.get("total_tokens", 0),
        )

        parsed_obj = None
        if response_model and content:
            try:
                parsed_obj = response_model.model_validate_json(content)
            except Exception as e:
                logger.warning(
                    f"Failed to validate response against {response_model.__name__}: {e}"
                )

        return LLMResponse(
            content=content,
            parsed=parsed_obj,
            tool_calls=tool_calls,
            usage=usage,
            provider="openai",
            raw=data,
        )

    def _execute_generate(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        if not self.api_key:
            raise LLMError("OPENAI_API_KEY is not set.")

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = self.build_payload(
            messages=messages,
            tools=tools,
            response_model=response_model,
            temperature=temperature,
        )

        try:
            with httpx.Client(timeout=self.timeout_sec) as client:
                res = client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                )
        except httpx.RequestError as e:
            raise LLMRetryableError(f"Network error communicating with OpenAI API: {e}") from e

        if res.status_code in (429, 500, 502, 503, 504):
            raise LLMRetryableError(f"OpenAI API status {res.status_code}: {res.text}")
        if res.status_code != 200:
            raise LLMError(f"OpenAI API error ({res.status_code}): {res.text}")

        data = res.json()
        return self.parse_openai_response(data, response_model=response_model)


def get_llm_client(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    **kwargs: Any,
) -> LLMClient:
    """Factory function to get an LLM client instance."""
    if provider is None:
        env_provider = os.getenv("LLM_PROVIDER")
        if env_provider:
            provider = env_provider.lower()
        elif os.getenv("GEMINI_API_KEY"):
            provider = "gemini"
        elif os.getenv("OPENAI_API_KEY"):
            provider = "openai"
        elif "PYTEST_CURRENT_TEST" in os.environ:
            provider = "fake"
        else:
            raise RuntimeError(
                "No LLM API key configured. GEMINI_API_KEY is required in .env for live autonomy. "
                "FakeLLM may exist only in tests/ for unit-testing plumbing, never in demos or evals."
            )

    provider = provider.lower()

    if provider == "gemini":
        resolved_key = api_key or os.getenv("GEMINI_API_KEY")
        if not resolved_key:
            raise RuntimeError("GEMINI_API_KEY is required for GeminiLLMClient. Check your .env file.")
        resolved_model = model or os.getenv("LLM_MODEL") or "gemini-3.5-flash-lite"
        return GeminiLLMClient(model=resolved_model, api_key=resolved_key, **kwargs)
    elif provider == "openai":
        resolved_key = api_key or os.getenv("OPENAI_API_KEY")
        if not resolved_key:
            raise RuntimeError("OPENAI_API_KEY is required for OpenAILLMClient. Check your .env file.")
        resolved_model = model or os.getenv("LLM_MODEL") or "gpt-4o-mini"
        return OpenAILLMClient(model=resolved_model, api_key=resolved_key, **kwargs)
    elif provider == "fake":
        if "PYTEST_CURRENT_TEST" not in os.environ and not kwargs.get("allow_fake_for_testing", False):
            raise RuntimeError(
                "FakeLLM may exist only in tests/ for unit-testing plumbing, never in demos, evals, or CLI execution."
            )
        from core.llm.fake import FakeLLM

        return FakeLLM(model=model or "fake-model", **kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")
