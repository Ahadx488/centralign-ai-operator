from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from pydantic import BaseModel

from core.llm.client import LLMClient
from core.llm.models import LLMMessage, LLMResponse, TokenUsage, ToolDefinition

T = TypeVar("T", bound=BaseModel)


class FakeLLM(LLMClient):
    """
    Deterministic, offline LLM mock for unit testing, fast evals, and dry-runs.
    Supports queued responses, pattern-based matching, structured output generation,
    tool call generation, and simulated retryable errors.
    """

    def __init__(
        self,
        model: str = "fake-model",
        default_response: str = "Default fake response",
        **kwargs,
    ):
        super().__init__(model=model, api_key="fake-key", **kwargs)
        self.default_response = default_response
        self._response_queue: list[
            LLMResponse | Exception | Callable[[list[LLMMessage]], LLMResponse]
        ] = []
        self._pattern_responses: dict[
            str, LLMResponse | Callable[[list[LLMMessage]], LLMResponse]
        ] = {}
        self.call_history: list[list[LLMMessage]] = []

    def queue_response(
        self, response: LLMResponse | Exception | Callable[[list[LLMMessage]], LLMResponse]
    ) -> None:
        """Queue a response or exception to be returned on subsequent calls."""
        self._response_queue.append(response)

    def register_pattern(
        self,
        substring_or_keyword: str,
        response: LLMResponse | Callable[[list[LLMMessage]], LLMResponse],
    ) -> None:
        """Register a response to return if any message in the prompt contains substring_or_keyword."""
        self._pattern_responses[substring_or_keyword] = response

    def clear(self) -> None:
        """Clear queued responses and history."""
        self._response_queue.clear()
        self._pattern_responses.clear()
        self.call_history.clear()

    def _execute_generate(
        self,
        messages: list[LLMMessage],
        tools: list[ToolDefinition] | None = None,
        response_model: type[T] | None = None,
        temperature: float = 0.2,
    ) -> LLMResponse:
        self.call_history.append(messages)

        # 1. Check if an item is in the response queue
        if self._response_queue:
            item = self._response_queue.pop(0)
            if isinstance(item, Exception):
                raise item
            if callable(item):
                return item(messages)
            if isinstance(item, LLMResponse):
                return item

        # 2. Check pattern matching
        all_text = " ".join([m.content for m in messages if m.content])
        for pattern, resp in self._pattern_responses.items():
            if pattern in all_text:
                if callable(resp):
                    return resp(messages)
                return resp

        # 3. If response_model was requested and no explicit mock was provided,
        # create a dummy instance if possible, or return empty JSON
        if response_model is not None:
            # Return dummy json valid for model if possible
            try:
                # Attempt to create default model instance or serialize empty schema
                dummy = response_model.model_construct()
                return LLMResponse(
                    content=dummy.model_dump_json(),
                    parsed=dummy,
                    usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
                    provider="fake",
                )
            except Exception:
                pass

        return LLMResponse(
            content=self.default_response,
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            provider="fake",
        )
