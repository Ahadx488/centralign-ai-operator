from __future__ import annotations

import logging
import os
import time

import pytest
from pydantic import BaseModel, Field

from core.llm.client import (
    GeminiLLMClient,
    LLMError,
    LLMRetryableError,
    OpenAILLMClient,
    get_llm_client,
    pydantic_to_gemini_schema,
)
from core.llm.fake import FakeLLM
from core.llm.models import LLMMessage, LLMResponse, TokenUsage, ToolCall, ToolDefinition


class HelloWorldOutput(BaseModel):
    greeting: str
    recipient: str
    status: str
    code: int = Field(default=200)


class LineItem(BaseModel):
    description: str
    amount: float


class ComplexInvoice(BaseModel):
    vendor_name: str
    invoice_number: str
    total_amount: float
    currency: str
    due_date: str
    items: list[LineItem] = Field(default_factory=list)
    notes: str | None = None


# ---------------------------------------------------------------------------
# 1. Basic Generation & Logging of Tokens and Latency
# ---------------------------------------------------------------------------


def test_fake_llm_basic_generation_and_logging(caplog):
    """Verify basic generation and that token usage and latency are logged on every call."""
    client = FakeLLM(default_response="CentrAlign AI worker ready.")
    messages = [LLMMessage(role="user", content="Hello, system status?")]

    with caplog.at_level(logging.INFO):
        response = client.generate(messages=messages)

    assert response.content == "CentrAlign AI worker ready."
    assert response.provider == "fake"
    assert response.latency_ms >= 0.0
    assert response.usage.total_tokens > 0

    # Verify that logging requirement from Phase 0 is genuinely satisfied
    log_records = [r.message for r in caplog.records if "LLM call completed" in r.message]
    assert len(log_records) == 1
    log_text = log_records[0]
    assert "provider=fake" in log_text
    assert "latency=" in log_text
    assert "prompt_tokens=" in log_text
    assert "completion_tokens=" in log_text
    assert "total_tokens=" in log_text


# ---------------------------------------------------------------------------
# 2. Hello World & Structured Output Validation
# ---------------------------------------------------------------------------


def test_hello_world_validated_json():
    """Done when check: a hello-world LLM call returns validated JSON."""
    client = FakeLLM()
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


def test_markdown_wrapped_json_parsing():
    """Ensure JSON wrapped in markdown fences (```json ... ```) is automatically parsed."""
    client = FakeLLM()
    client.queue_response(
        LLMResponse(
            content="""```json
            {
                "greeting": "Hi",
                "recipient": "Operator",
                "status": "ready",
                "code": 201
            }
            ```""",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=20, total_tokens=30),
            provider="fake",
        )
    )

    response = client.generate(
        messages=[LLMMessage(role="user", content="Handshake")],
        response_model=HelloWorldOutput,
    )

    assert response.parsed is not None
    assert response.parsed.greeting == "Hi"
    assert response.parsed.code == 201


def test_pydantic_validation_failure_handling():
    """Verify behavior when LLM output violates schema or is malformed."""
    # Case 1: Malformed JSON syntax
    bad_syntax_resp = LLMResponse(content='{"greeting": "broken"', provider="fake")
    with pytest.raises(ValueError, match="Content is not valid JSON"):
        bad_syntax_resp.get_parsed(HelloWorldOutput)

    # Case 2: Valid JSON but missing required fields
    missing_fields_resp = LLMResponse(content='{"greeting": "hi"}', provider="fake")
    with pytest.raises(ValueError, match="Failed to validate JSON against HelloWorldOutput"):
        missing_fields_resp.get_parsed(HelloWorldOutput)

    # Case 3: In generate(), invalid JSON results in parsed being None with warning
    client = FakeLLM()
    client.queue_response(LLMResponse(content='{"invalid_field": 123}', provider="fake"))
    res = client.generate(
        messages=[LLMMessage(role="user", content="test")],
        response_model=HelloWorldOutput,
    )
    assert res.parsed is None


# ---------------------------------------------------------------------------
# 3. Tool Calling & Response Parsing for Gemini & OpenAI
# ---------------------------------------------------------------------------


def test_fake_llm_tool_calling():
    """Verify tool call dispatch and argument structure."""
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


def test_gemini_response_payload_parser():
    """Verify Gemini-specific JSON response parsing (text, functionCall, usageMetadata)."""
    gemini_client = GeminiLLMClient(api_key="mock_key")
    raw_gemini_payload = {
        "candidates": [
            {
                "content": {
                    "role": "model",
                    "parts": [
                        {
                            "functionCall": {
                                "name": "download_invoice",
                                "args": {"vendor": "Apex Ltd", "invoice_id": "INV-101"},
                            }
                        }
                    ],
                }
            }
        ],
        "usageMetadata": {
            "promptTokenCount": 42,
            "candidatesTokenCount": 18,
            "totalTokenCount": 60,
        },
    }

    parsed = gemini_client.parse_gemini_response(raw_gemini_payload)

    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].name == "download_invoice"
    assert parsed.tool_calls[0].arguments == {"vendor": "Apex Ltd", "invoice_id": "INV-101"}
    assert parsed.usage.prompt_tokens == 42
    assert parsed.usage.completion_tokens == 18
    assert parsed.usage.total_tokens == 60


def test_openai_response_payload_parser():
    """Verify OpenAI-specific JSON response parsing (choices, tool_calls, usage)."""
    openai_client = OpenAILLMClient(api_key="mock_key")
    raw_openai_payload = {
        "choices": [
            {
                "message": {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": "call_abc123",
                            "type": "function",
                            "function": {
                                "name": "create_bill",
                                "arguments": '{"amount": 5500.0, "currency": "INR"}',
                            },
                        }
                    ],
                }
            }
        ],
        "usage": {
            "prompt_tokens": 50,
            "completion_tokens": 20,
            "total_tokens": 70,
        },
    }

    parsed = openai_client.parse_openai_response(raw_openai_payload)

    assert len(parsed.tool_calls) == 1
    assert parsed.tool_calls[0].id == "call_abc123"
    assert parsed.tool_calls[0].name == "create_bill"
    assert parsed.tool_calls[0].arguments == {"amount": 5500.0, "currency": "INR"}
    assert parsed.usage.total_tokens == 70


# ---------------------------------------------------------------------------
# 4. Exponential Backoff Timing & Retry Logic
# ---------------------------------------------------------------------------


def test_fake_llm_exponential_backoff_timing():
    """Verify genuine exponential backoff delay between retry attempts."""
    initial_backoff = 0.05
    client = FakeLLM(max_retries=3, initial_backoff_sec=initial_backoff, backoff_multiplier=2.0)

    # Queue two transient failures before success (sleep 0.05s, then 0.10s -> total >= 0.15s)
    client.queue_response(LLMRetryableError("503 Gateway Timeout"))
    client.queue_response(LLMRetryableError("429 Too Many Requests"))
    client.queue_response(
        LLMResponse(
            content="Recovered after backoff",
            usage=TokenUsage(prompt_tokens=10, completion_tokens=10, total_tokens=20),
            provider="fake",
        )
    )

    start = time.time()
    response = client.generate(messages=[LLMMessage(role="user", content="Retry timing test")])
    elapsed = time.time() - start

    assert response.content == "Recovered after backoff"
    assert len(client.call_history) == 3
    # Total delay should be at least initial_backoff (0.05) + backoff*multiplier (0.10) = 0.15s
    assert elapsed >= 0.14, f"Expected backoff >= 0.14s, actual was {elapsed:.3f}s"


def test_fake_llm_max_retries_exceeded():
    """Verify that persistent retryable errors raise LLMError when retries are exhausted."""
    client = FakeLLM(max_retries=2, initial_backoff_sec=0.01)
    client.queue_response(LLMRetryableError("Rate limit exceeded"))
    client.queue_response(LLMRetryableError("Rate limit exceeded again"))

    with pytest.raises(LLMError, match="Exceeded max retries"):
        client.generate(messages=[LLMMessage(role="user", content="Test failure")])


# ---------------------------------------------------------------------------
# 5. Gemini API Spec Schema Conversion & Request Formatting
# ---------------------------------------------------------------------------


def test_gemini_schema_conversion_complex_nested_model():
    """Verify Pydantic models with nested objects, arrays, and optionals format properly for Gemini."""
    gemini_schema = pydantic_to_gemini_schema(ComplexInvoice)

    # Must be uppercase OBJECT
    assert gemini_schema["type"] == "OBJECT"
    assert "properties" in gemini_schema

    # String, Number, Array types must be uppercase
    assert gemini_schema["properties"]["vendor_name"]["type"] == "STRING"
    assert gemini_schema["properties"]["total_amount"]["type"] == "NUMBER"

    # Nested array of objects ($defs resolved inline, no $defs in output)
    items_schema = gemini_schema["properties"]["items"]
    assert items_schema["type"] == "ARRAY"
    assert "$defs" not in gemini_schema
    assert "$defs" not in items_schema
    assert items_schema["items"]["type"] == "OBJECT"
    assert items_schema["items"]["properties"]["description"]["type"] == "STRING"
    assert items_schema["items"]["properties"]["amount"]["type"] == "NUMBER"

    # Optional field (notes: str | None)
    notes_schema = gemini_schema["properties"]["notes"]
    assert notes_schema["type"] == "STRING"
    assert notes_schema.get("nullable") is True


def test_gemini_rest_payload_builder():
    """Verify complete Gemini REST payload complies with v1beta endpoint specifications."""
    client = GeminiLLMClient(api_key="mock_gemini_key")
    messages = [
        LLMMessage(role="system", content="System instruction context"),
        LLMMessage(role="user", content="Extract invoice INV-01"),
    ]
    tools = [
        ToolDefinition(
            name="fetch_invoice",
            description="Fetch invoice data",
            parameters={
                "type": "object",
                "properties": {"invoice_id": {"type": "string"}},
                "required": ["invoice_id"],
            },
        )
    ]

    payload = client.build_payload(
        messages=messages,
        tools=tools,
        response_model=ComplexInvoice,
        temperature=0.1,
    )

    # 1. System instruction formatted separately per Gemini API
    assert "systemInstruction" in payload
    assert payload["systemInstruction"]["parts"][0]["text"] == "System instruction context"

    # 2. Contents contain user prompt
    assert payload["contents"][0]["role"] == "user"
    assert payload["contents"][0]["parts"][0]["text"] == "Extract invoice INV-01"

    # 3. GenerationConfig has application/json and Gemini Schema
    gen_config = payload["generationConfig"]
    assert gen_config["responseMimeType"] == "application/json"
    assert gen_config["responseSchema"]["type"] == "OBJECT"
    assert "vendor_name" in gen_config["responseSchema"]["properties"]

    # 4. Function declarations format
    assert len(payload["tools"]) == 1
    fn_decl = payload["tools"][0]["functionDeclarations"][0]
    assert fn_decl["name"] == "fetch_invoice"
    assert fn_decl["parameters"]["type"] == "OBJECT"


# ---------------------------------------------------------------------------
# 6. Factory & Live Gemini Smoke Test
# ---------------------------------------------------------------------------


def test_get_llm_client_factory(monkeypatch):
    monkeypatch.delenv("LLM_MODEL", raising=False)
    monkeypatch.setenv("LLM_PROVIDER", "fake")
    client = get_llm_client()
    assert isinstance(client, FakeLLM)

    monkeypatch.setenv("LLM_PROVIDER", "gemini")
    monkeypatch.setenv("GEMINI_API_KEY", "test-key")
    gemini_client = get_llm_client()
    assert gemini_client.model == "gemini-3.5-flash-lite"
    assert gemini_client.api_key == "test-key"


@pytest.mark.skipif(
    not os.getenv("GEMINI_API_KEY"), reason="GEMINI_API_KEY not configured in environment"
)
def test_live_gemini_smoke():
    """Live smoke test against real Gemini API when GEMINI_API_KEY is available."""
    client = GeminiLLMClient()
    messages = [
        LLMMessage(role="system", content="You are a JSON test agent."),
        LLMMessage(
            role="user",
            content="Respond with greeting 'Hello Gemini', recipient 'CentrAlign', status 'ok', code 200",
        ),
    ]
    response = client.generate(messages=messages, response_model=HelloWorldOutput)
    assert response.parsed is not None
    assert isinstance(response.parsed, HelloWorldOutput)
    assert response.parsed.code == 200
    assert response.usage.total_tokens > 0
    assert response.latency_ms > 0
