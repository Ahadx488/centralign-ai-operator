from __future__ import annotations

import abc
import json
import logging
import os
import time
from typing import Any, TypeVar

import httpx
from pydantic import BaseModel

from core.llm.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolDefinition

logger = logging.getLogger(__name__)

T = TypeVar("T", bound=BaseModel)


class LLMError(Exception):
    """Base exception for LLM errors."""


class LLMRetryableError(LLMError):
    """Error that is safe to retry (e.g. rate limit, 503, transient network)."""


class LLMClient(abc.ABC):
    """
    Provider-agnostic LLM Client interface.
    Supports structured output (Pydantic), tool calling, and retries with backoff.
    """

    def __init__(
        self,
        model: str,
        api_key: str | None = None,
        max_retries: int = 3,
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

                return response

            except LLMRetryableError as e:
                last_error = e
                logger.warning(
                    f"Attempt {attempt}/{self.max_retries} failed with retryable error: {e}"
                )
                if attempt < self.max_retries:
                    time.sleep(backoff)
                    backoff *= self.backoff_multiplier
            except Exception as e:
                # Non-retryable error
                logger.error(f"LLM call failed with non-retryable error: {e}")
                raise

        raise LLMError(f"Exceeded max retries ({self.max_retries}): {last_error}")

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
    Direct Gemini API client via httpx.
    Supports Gemini 1.5/2.0/2.5 models, JSON mode, and function calling.
    """

    def __init__(
        self,
        model: str = "gemini-2.5-flash",
        api_key: str | None = None,
        **kwargs,
    ):
        api_key = api_key or os.getenv("GEMINI_API_KEY")
        super().__init__(model=model, api_key=api_key, **kwargs)
        self.base_url = "https://generativelanguage.googleapis.com/v1beta/models"

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

        # Convert LLMMessage to Gemini contents format
        contents = []
        system_instruction = None

        for msg in messages:
            if msg.role == "system":
                system_instruction = {"parts": [{"text": msg.content}]}
            elif msg.role == "user":
                contents.append({"role": "user", "parts": [{"text": msg.content}]})
            elif msg.role == "assistant":
                parts = []
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
            # Schema definition can also be provided if supported
            schema = response_model.model_json_schema()
            payload["generationConfig"]["responseSchema"] = schema

        if tools:
            function_declarations = []
            for t in tools:
                function_declarations.append(
                    {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.parameters,
                    }
                )
            payload["tools"] = [{"functionDeclarations": function_declarations}]

        try:
            with httpx.Client(timeout=self.timeout_sec) as client:
                res = client.post(url, json=payload)
        except httpx.RequestError as e:
            raise LLMRetryableError(f"Network error communicating with Gemini API: {e}")

        if res.status_code in (429, 500, 502, 503, 504):
            raise LLMRetryableError(f"Gemini API returned status {res.status_code}: {res.text}")
        if res.status_code != 200:
            raise LLMError(f"Gemini API error ({res.status_code}): {res.text}")

        data = res.json()
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

        # Usage metadata
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


class OpenAILLMClient(LLMClient):
    """
    OpenAI API client via httpx.
    """

    def __init__(
        self,
        model: str = "gpt-4o-mini",
        api_key: str | None = None,
        base_url: str = "https://api.openai.com/v1",
        **kwargs,
    ):
        api_key = api_key or os.getenv("OPENAI_API_KEY")
        super().__init__(model=model, api_key=api_key, **kwargs)
        self.base_url = base_url

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

        openai_messages = []
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

        try:
            with httpx.Client(timeout=self.timeout_sec) as client:
                res = client.post(
                    f"{self.base_url}/chat/completions", headers=headers, json=payload
                )
        except httpx.RequestError as e:
            raise LLMRetryableError(f"Network error communicating with OpenAI API: {e}")

        if res.status_code in (429, 500, 502, 503, 504):
            raise LLMRetryableError(f"OpenAI API status {res.status_code}: {res.text}")
        if res.status_code != 200:
            raise LLMError(f"OpenAI API error ({res.status_code}): {res.text}")

        data = res.json()
        choice = data["choices"][0]["message"]
        content = choice.get("content") or ""
        tool_calls: list[ToolCall] = []

        if choice.get("tool_calls"):
            for tc in choice["tool_calls"]:
                fn = tc.get("function", {})
                args = {}
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


def get_llm_client(
    provider: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    **kwargs,
) -> LLMClient:
    """Factory function to get an LLM client instance."""
    provider = provider or os.getenv("LLM_PROVIDER", "fake").lower()

    if provider == "gemini":
        model = model or os.getenv("LLM_MODEL", "gemini-2.5-flash")
        return GeminiLLMClient(model=model, api_key=api_key, **kwargs)
    elif provider == "openai":
        model = model or os.getenv("LLM_MODEL", "gpt-4o-mini")
        return OpenAILLMClient(model=model, api_key=api_key, **kwargs)
    elif provider == "fake":
        from core.llm.fake import FakeLLM

        return FakeLLM(model=model or "fake-model", **kwargs)
    else:
        raise ValueError(f"Unsupported LLM provider: {provider}")
