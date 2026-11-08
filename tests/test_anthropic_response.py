"""Anthropic Messages calls: request shape and response parsing.

Thinking is always on for the current models, so a response can open with a
thinking block. Text must be read by block type, and refusals must not parse.
"""

import json

import httpx
import pytest

from daisy import constants as c
from daisy.config import settings
from daisy.workers import extraction

CLAIMS = [{"claim_type": "email", "value": "a@example.com"}]


def _patch_client(monkeypatch, body: dict, sent: list):
    def handler(request: httpx.Request) -> httpx.Response:
        sent.append(json.loads(request.content))
        return httpx.Response(200, json=body)

    real = httpx.AsyncClient

    def fake_client(*args, **kwargs):
        kwargs["transport"] = httpx.MockTransport(handler)
        return real(*args, **kwargs)

    monkeypatch.setattr(extraction.httpx, "AsyncClient", fake_client)
    monkeypatch.setattr(settings, "anthropic_api_key", "test-key")


@pytest.mark.asyncio
async def test_text_read_after_leading_thinking_block(monkeypatch):
    sent: list = []
    body = {
        "stop_reason": "end_turn",
        "content": [
            {"type": "thinking", "thinking": "", "signature": "x"},
            {"type": "text", "text": json.dumps(CLAIMS)},
        ],
    }
    _patch_client(monkeypatch, body, sent)

    result = await extraction._extract_claims_llm(
        "text", "Subject", c.DEFAULT_EXTRACTION_MODEL,
    )

    assert result == CLAIMS
    assert sent[0]["model"] == c.DEFAULT_EXTRACTION_MODEL
    assert sent[0]["max_tokens"] == c.DEFAULT_LLM_MAX_TOKENS
    assert sent[0]["output_config"] == {"effort": c.DEFAULT_LLM_EFFORT}


@pytest.mark.asyncio
async def test_refusal_returns_no_claims(monkeypatch):
    sent: list = []
    body = {
        "stop_reason": "refusal",
        "stop_details": {"type": "refusal", "category": "general_harms"},
        "content": [],
    }
    _patch_client(monkeypatch, body, sent)

    result = await extraction._extract_claims_llm(
        "text", "Subject", c.DEFAULT_EXTRACTION_MODEL,
    )

    assert result == []
