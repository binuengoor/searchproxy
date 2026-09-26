"""Tests for structured JSON extraction endpoint (POST /v1/extract) and ExtractService."""
from __future__ import annotations

import json
from unittest.mock import AsyncMock, MagicMock

import pytest

from app.config import Settings
from app.dependencies import get_extract_service
from app.main import app as fastapi_app
from app.schemas import ExtractResponse
from app.services.extract_service import ExtractService
from app.services.models import FetchResult

# ---------------------------------------------------------------------------
# Unit tests for ExtractService
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_extract_service_success_with_schema():
    """ExtractService extracts JSON and validates against schema."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://news.ycombinator.com",
        markdown="# Hacker News\n\n1. Claude 3.7 Released (512 points)",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": json.dumps({"top_story": "Claude 3.7 Released", "points": 512})
                }
            }
        ]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    settings = Settings(
        LLM_CHAT_URL="https://api.groq.com/openai/v1/chat/completions",
        LLM_CHAT_MODEL="test-model",
        LLM_API_KEY="test-key",
    )

    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=settings)
    schema = {
        "type": "object",
        "properties": {
            "top_story": {"type": "string"},
            "points": {"type": "integer"},
        },
        "required": ["top_story", "points"],
    }

    result = await service.extract(
        url="https://news.ycombinator.com",
        schema=schema,
        prompt="Extract top story and points",
    )

    assert result.success is True
    assert result.data == {"top_story": "Claude 3.7 Released", "points": 512}
    assert result.url == "https://news.ycombinator.com"
    assert result.error is None


@pytest.mark.anyio
async def test_extract_service_without_schema():
    """ExtractService extracts unstructured JSON when no schema is given."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Page\n\nSome text content.",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"summary": "text content"})}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    settings = Settings()
    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=settings)

    result = await service.extract(url="https://example.com", prompt="Summarize as JSON")
    assert result.success is True
    assert result.data == {"summary": "text content"}


@pytest.mark.anyio
async def test_extract_service_strips_markdown_fences():
    """ExtractService automatically strips ```json ... ``` formatting."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Test",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [
            {
                "message": {
                    "content": "```json\n{\"item\": \"laptop\", \"price\": 999}\n```"
                }
            }
        ]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    settings = Settings()
    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=settings)

    result = await service.extract(url="https://example.com")
    assert result.success is True
    assert result.data == {"item": "laptop", "price": 999}


@pytest.mark.anyio
async def test_extract_service_fetch_failure():
    """If fetch chain fails, ExtractService returns success=false with fetch error."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=False,
        url="https://blocked.example.com",
        error="All fetch tiers exhausted; Cloudflare blocked.",
    )

    service = ExtractService(fetch_chain=fetch_chain, http_client=AsyncMock(), settings=Settings())
    result = await service.extract(url="https://blocked.example.com")

    assert result.success is False
    assert result.data is None
    assert "Cloudflare blocked" in result.error


@pytest.mark.anyio
async def test_extract_service_empty_content():
    """If fetched content is blank, ExtractService returns success=false."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://empty.example.com",
        markdown="   ",
    )

    service = ExtractService(fetch_chain=fetch_chain, http_client=AsyncMock(), settings=Settings())
    result = await service.extract(url="https://empty.example.com")

    assert result.success is False
    assert "no extractable content" in result.error


@pytest.mark.anyio
async def test_extract_service_schema_validation_failure():
    """Schema violations result in success=false with schema validation message."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Product",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"title": "Sample"})}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    schema = {
        "type": "object",
        "properties": {
            "title": {"type": "string"},
            "price": {"type": "number"},
        },
        "required": ["title", "price"],
    }

    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=Settings())
    result = await service.extract(url="https://example.com", schema=schema)

    assert result.success is False
    assert "Schema validation error" in result.error
    assert result.data == {"title": "Sample"}


@pytest.mark.anyio
async def test_extract_service_invalid_llm_json():
    """Invalid JSON output from LLM results in success=false with parse failure reason."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Test",
    )

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "Sorry, I cannot extract JSON."}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=Settings())
    result = await service.extract(url="https://example.com")

    assert result.success is False
    assert "Failed to parse LLM output as JSON" in result.error


@pytest.mark.anyio
async def test_extract_service_retry_without_response_format():
    """If provider returns HTTP 400 for response_format, retries without it."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Test",
    )

    resp_400 = MagicMock()
    resp_400.status_code = 400

    resp_200 = MagicMock()
    resp_200.status_code = 200
    resp_200.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"recovered": True})}}]
    }

    http_client = AsyncMock()
    http_client.post.side_effect = [resp_400, resp_200]

    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=Settings())
    result = await service.extract(url="https://example.com")

    assert result.success is True
    assert result.data == {"recovered": True}
    assert http_client.post.call_count == 2


# ---------------------------------------------------------------------------
# Router & Endpoint Tests
# ---------------------------------------------------------------------------

@pytest.fixture
def mock_extract_service():
    mock = MagicMock()
    mock.extract = AsyncMock(
        return_value=ExtractResponse(
            success=True,
            data={"test": "data"},
            url="https://example.com",
            error=None,
        )
    )
    fastapi_app.dependency_overrides[get_extract_service] = lambda: mock
    yield mock
    fastapi_app.dependency_overrides.clear()


@pytest.mark.anyio
async def test_v1_extract_endpoint(client, mock_extract_service):
    """POST /v1/extract returns ExtractResponse matching service output."""
    resp = await client.post(
        "/v1/extract",
        json={"url": "https://example.com", "schema": {"type": "object"}, "prompt": "Extract"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["success"] is True
    assert data["data"] == {"test": "data"}
    assert data["url"] == "https://example.com"
    mock_extract_service.extract.assert_awaited_once_with(
        url="https://example.com",
        schema={"type": "object"},
        prompt="Extract",
        system_prompt=None,
    )


@pytest.mark.anyio
async def test_v1_extract_trailing_slash(client, mock_extract_service):
    """POST /v1/extract/ works with trailing slash."""
    resp = await client.post("/v1/extract/", json={"url": "https://example.com"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    mock_extract_service.extract.assert_awaited_once()


@pytest.mark.anyio
async def test_compat_firecrawl_v1_extract(client, mock_extract_service):
    """POST /compat/firecrawl/v1/extract works for Firecrawl clients."""
    resp = await client.post("/compat/firecrawl/v1/extract", json={"url": "https://example.com"})
    assert resp.status_code == 200
    assert resp.json()["success"] is True
    mock_extract_service.extract.assert_awaited_once()


@pytest.mark.anyio
async def test_extract_nested_body_payload(client, mock_extract_service):
    """MCPHub / Open WebUI nested body wrappers are unwrapped cleanly."""
    resp = await client.post(
        "/v1/extract",
        json={
            "body": {
                "url": "https://wrapped.example.com",
                "schema": {"type": "object"},
                "prompt": "Wrapped prompt",
            }
        },
    )
    assert resp.status_code == 200
    mock_extract_service.extract.assert_awaited_once_with(
        url="https://wrapped.example.com",
        schema={"type": "object"},
        prompt="Wrapped prompt",
        system_prompt=None,
    )


@pytest.mark.anyio
async def test_extract_service_schema_error_handled():
    """Invalid JSON schema definition is handled gracefully without throwing 500 error."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Product",
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps({"title": "Sample"})}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=Settings())
    result = await service.extract(
        url="https://example.com",
        schema={"type": "invalid_type_name"},
    )
    assert result.success is False
    assert "Invalid JSON Schema" in result.error


@pytest.mark.anyio
async def test_extract_service_primitive_result():
    """ExtractService handles primitive extracted data conforming to primitive schemas."""
    fetch_chain = AsyncMock()
    fetch_chain.execute.return_value = FetchResult(
        success=True,
        url="https://example.com",
        markdown="# Title only",
    )
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": json.dumps("Simple Title")}}]
    }
    http_client = AsyncMock()
    http_client.post.return_value = mock_resp

    service = ExtractService(fetch_chain=fetch_chain, http_client=http_client, settings=Settings())
    result = await service.extract(
        url="https://example.com",
        schema={"type": "string"},
    )
    assert result.success is True
    assert result.data == "Simple Title"


@pytest.mark.anyio
async def test_extract_endpoint_urls_list_support(client, mock_extract_service):
    """Firecrawl-compatible 'urls' list payload is mapped to 'url'."""
    resp = await client.post(
        "/v1/extract",
        json={"urls": ["https://firecrawl-list.example.com"], "prompt": "Extract"},
    )
    assert resp.status_code == 200
    mock_extract_service.extract.assert_awaited_once_with(
        url="https://firecrawl-list.example.com",
        schema=None,
        prompt="Extract",
        system_prompt=None,
    )


@pytest.mark.anyio
async def test_extract_endpoint_system_prompt_camel_case(client, mock_extract_service):
    """Firecrawl-compatible camelCase 'systemPrompt' is recognized."""
    resp = await client.post(
        "/v1/extract",
        json={
            "url": "https://example.com",
            "systemPrompt": "Custom system instructions",
        },
    )
    assert resp.status_code == 200
    mock_extract_service.extract.assert_awaited_once_with(
        url="https://example.com",
        schema=None,
        prompt=None,
        system_prompt="Custom system instructions",
    )


@pytest.mark.anyio
async def test_extract_endpoint_string_schema_parsed(client, mock_extract_service):
    """String-serialized JSON schema is parsed to dict."""
    resp = await client.post(
        "/v1/extract",
        json={
            "url": "https://example.com",
            "schema": json.dumps({"type": "object", "properties": {"a": {"type": "string"}}}),
        },
    )
    assert resp.status_code == 200
    mock_extract_service.extract.assert_awaited_once_with(
        url="https://example.com",
        schema={"type": "object", "properties": {"a": {"type": "string"}}},
        prompt=None,
        system_prompt=None,
    )
