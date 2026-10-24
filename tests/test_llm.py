from __future__ import annotations

import pytest
from pydantic import BaseModel, Field

from core.llm.client import LLMError, LLMRetryableError, get_llm_client
from core.llm.fake import FakeLLM
from core.llm.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolDefinition


class HelloWorldOutput(BaseModel):
    greeting: str
    recipient: str
    status: str
    code: int = Field(default=200)


class InvoiceExtraction(BaseModel):
    vendor_name: str
    invoice_number: str
    amount: float
    currency: str
    due_date: str


def test_fake_llm_basic_generation():
    client = FakeLLM(default_response="CentrAlign AI worker ready.")
    messages = [LLMMessage(role="user", content="Hello, system status?")]
    response = client.generate(messages=messages)

    assert response.content == "CentrAlign AI worker ready."
    assert response.provider == "fake"
    assert response.latency_ms >= 0.0
    assert response.usage.total_tokens > 0
    assert len(client.call_history) == 1
    assert client.call_history[0][0].content == "Hello, system status?"


def test_hello_world_validated_json():
    """Done when check: a hello-world LLM call returns validated JSON."""
    client = FakeLLM()
    # Queue a response that produces valid JSON matching HelloWorldOutput
    client.queue_response(
        LLMResponse(
            content='{"greeting": "Hello", "recipient": "CentrAlign", "status": "operational", "code": 200}',
            usage=TokenUsage(prompt_tokens=15, completion_tokens=25, total_tokens=40),
            provider="fake",
        )
    )

    messages = [
        LLMMessage(role="system", content="You are a helpful assistant."),
        LLMMessage(role="user", content="Provide system handshake."),
    ]

    response = client.generate(messages=messages, response_model=HelloWorldOutput)

    assert response.parsed is not None
    assert isinstance(response.parsed, HelloWorldOutput)
    assert response.parsed.greeting == "Hello"
    assert response.parsed.recipient == "CentrAlign"
    assert response.parsed.status == "operational"
    assert response.parsed.code == 200


def test_fake_llm_structured_extraction():
    client = FakeLLM()
    client.queue_response(
        LLMResponse(
            content="""```json
            {
                "vendor_name": "Acme Supplies Inc",
                "invoice_number": "INV-2026-0042",
                "amount": 4500.50,
                "currency": "INR",
                "due_date": "2026-11-15"
            }
            ```""",
            usage=TokenUsage(prompt_tokens=50, completion_tokens=30, total_tokens=80),
            provider="fake",
        )
    )

    response = client.generate(
        messages=[LLMMessage(role="user", content="Extract invoice details")],
        response_model=InvoiceExtraction,
    )

    assert response.parsed is not None
    assert response.parsed.vendor_name == "Acme Supplies Inc"
    assert response.parsed.invoice_number == "INV-2026-0042"
    assert response.parsed.amount == 4500.50
    assert response.parsed.currency == "INR"


def test_fake_llm_tool_calling():
    client = FakeLLM()
    tool_call = ToolCall(
        id="call_browser_01",
        name="browser_open",
        arguments={"url": "http://localhost:8001/login"},
    )
    client.queue_response(
        LLMResponse(
            content="",
            tool_calls=[tool_call],
            usage=TokenUsage(prompt_tokens=20, completion_tokens=15, total_tokens=35),
            provider="fake",
        )
    )

    tools = [
        ToolDefinition(
            name="browser_open",
            description="Open a browser to the target URL",
            parameters={
                "type": "object",
                "properties": {"url": {"type": "string"}},
                "required": ["url"],
            },
        )
    ]

    response = client.generate(
        messages=[LLMMessage(role="user", content="Open the ERP login page")],
        tools=tools,
    )

    assert len(response.tool_calls) == 1
    assert response.tool_calls[0].name == "browser_open"
    assert response.tool_calls[0].arguments["url"] == "http://localhost:8001/login"


def test_fake_llm_retry_and_backoff():
    """Verify that transient retryable errors retry and succeed."""
    client = FakeLLM(max_retries=3, initial_backoff_sec=0.01)

    # Queue a transient failure first, then a success
    client.queue_response(LLMRetryableError("Transient 503 Service Unavailable"))
    client.queue_response(
        LLMResponse(
            content="Recovered after retry",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            provider="fake",
        )
    )

    response = client.generate(messages=[LLMMessage(role="user", content="Test retry")])
    assert response.content == "Recovered after retry"
    assert len(client.call_history) == 2


def test_fake_llm_max_retries_exceeded():
    """Verify that persistent retryable errors raise LLMError when retries are exhausted."""
    client = FakeLLM(max_retries=2, initial_backoff_sec=0.01)
    client.queue_response(LLMRetryableError("Rate limit exceeded"))
    client.queue_response(LLMRetryableError("Rate limit exceeded again"))

    with pytest.raises(LLMError, match="Exceeded max retries"):
        client.generate(messages=[LLMMessage(role="user", content="Test failure")])


def test_get_llm_client_factory(monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    client = get_llm_client()
    assert isinstance(client, FakeLLM)

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    gemini_client = get_llm_client()
    assert gemini_client.model == "gemini-2.5-flash"
    assert gemini_client.api_key == "test-key"
