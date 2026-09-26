"""Tests for Native Model Context Protocol (MCP) server endpoints and tools."""
from __future__ import annotations

import socket
import threading
import time
from unittest.mock import AsyncMock, MagicMock

import pytest
import uvicorn
from mcp import ClientSession
from mcp.client.sse import sse_client

from app.main import app as fastapi_app
from app.mcp_server import create_mcp_server
from app.schemas import Citation, RetrieveResponse, SourceChunk
from app.services.models import FetchResult

# ---------------------------------------------------------------------------
# Tool Registration & Schema Tests
# ---------------------------------------------------------------------------

def test_mcp_tools_registered():
    """All required SearchProxy tools and aliases are registered on MCPServer."""
    server = create_mcp_server()
    # Check registered tool names
    tool_names = set(server._tool_manager._tools.keys())
    expected = {
        "searchproxy_retrieve",
        "web_search",
        "searchproxy_fetch",
        "web_fetch",
        "searchproxy_research",
        "deep_research",
    }
    assert expected.issubset(tool_names), f"Missing tools: {expected - tool_names}"


# ---------------------------------------------------------------------------
# Direct Tool Invocation Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_tool_searchproxy_retrieve_synthesize(monkeypatch):
    """searchproxy_retrieve tool produces formatted answer with citations when synthesize=true."""
    mock_service = MagicMock()
    mock_service.retrieve = AsyncMock(
        return_value=RetrieveResponse(
            query="Python 3.13 features",
            answer="Python 3.13 introduces a free-threaded build [1].",
            citations=[
                Citation(
                    id=1,
                    url="https://docs.python.org/3.13/",
                    title="What's New In Python 3.13",
                    relevance_score=0.98,
                )
            ],
            sources=[],
            sources_fetched=1,
            sources_failed=0,
        )
    )
    monkeypatch.setattr("app.mcp_server.get_retrieve_service", lambda: mock_service)

    server = create_mcp_server()
    res = await server.call_tool(
        "searchproxy_retrieve",
        {"query": "Python 3.13 features", "synthesize": True},
    )

    assert not res.is_error
    text = res.content[0].text
    assert "Python 3.13 introduces a free-threaded build [1]." in text
    assert "### Sources" in text
    assert "[1] https://docs.python.org/3.13/ - What's New In Python 3.13" in text


@pytest.mark.anyio
async def test_tool_searchproxy_retrieve_raw_sources(monkeypatch):
    """searchproxy_retrieve tool returns raw ranked chunks when synthesize=false."""
    mock_service = MagicMock()
    mock_service.retrieve = AsyncMock(
        return_value=RetrieveResponse(
            query="Linux kernel",
            answer="",
            citations=[],
            sources=[
                SourceChunk(
                    url="https://kernel.org",
                    title="The Linux Kernel Archives",
                    content="Latest stable release...",
                    fetch_tier="crawl4ai",
                )
            ],
            sources_fetched=1,
            sources_failed=0,
        )
    )
    monkeypatch.setattr("app.mcp_server.get_retrieve_service", lambda: mock_service)

    server = create_mcp_server()
    res = await server.call_tool(
        "searchproxy_retrieve",
        {"query": "Linux kernel", "synthesize": False},
    )

    assert not res.is_error
    text = res.content[0].text
    assert "The Linux Kernel Archives" in text
    assert "https://kernel.org" in text
    assert "Latest stable release..." in text


@pytest.mark.anyio
async def test_tool_web_search_alias(monkeypatch):
    """web_search alias delegates to searchproxy_retrieve."""
    mock_service = MagicMock()
    mock_service.retrieve = AsyncMock(
        return_value=RetrieveResponse(
            query="test",
            answer="Answer [1]",
            citations=[Citation(id=1, url="https://example.com", title="Example")],
            sources=[],
            sources_fetched=1,
            sources_failed=0,
        )
    )
    monkeypatch.setattr("app.mcp_server.get_retrieve_service", lambda: mock_service)

    server = create_mcp_server()
    res = await server.call_tool("web_search", {"query": "test"})
    assert not res.is_error
    assert "Answer [1]" in res.content[0].text


@pytest.mark.anyio
async def test_tool_searchproxy_fetch_success(monkeypatch):
    """searchproxy_fetch fetches markdown and metadata from FetchChain."""
    mock_chain = MagicMock()
    mock_chain.execute = AsyncMock(
        return_value=FetchResult(
            success=True,
            url="https://example.com/doc",
            title="Documentation",
            description="Doc description",
            markdown="# Full Document Content",
            source="crawl4ai",
        )
    )
    monkeypatch.setattr("app.mcp_server.get_fetch_chain", lambda: mock_chain)

    server = create_mcp_server()
    res = await server.call_tool("searchproxy_fetch", {"url": "https://example.com/doc"})
    assert not res.is_error
    text = res.content[0].text
    assert "# Documentation" in text
    assert "https://example.com/doc" in text
    assert "> Doc description" in text
    assert "# Full Document Content" in text


@pytest.mark.anyio
async def test_tool_searchproxy_fetch_failure(monkeypatch):
    """searchproxy_fetch returns clear error message if fetch fails."""
    mock_chain = MagicMock()
    mock_chain.execute = AsyncMock(
        return_value=FetchResult(
            success=False,
            url="https://blocked.com",
            error="Cloudflare challenge unsolved",
        )
    )
    monkeypatch.setattr("app.mcp_server.get_fetch_chain", lambda: mock_chain)

    server = create_mcp_server()
    res = await server.call_tool("searchproxy_fetch", {"url": "https://blocked.com"})
    assert not res.is_error
    assert "Cloudflare challenge unsolved" in res.content[0].text


@pytest.mark.anyio
async def test_tool_web_fetch_alias(monkeypatch):
    """web_fetch alias delegates to searchproxy_fetch."""
    mock_chain = MagicMock()
    mock_chain.execute = AsyncMock(
        return_value=FetchResult(
            success=True,
            url="https://example.com",
            title="Home",
            markdown="Hello",
        )
    )
    monkeypatch.setattr("app.mcp_server.get_fetch_chain", lambda: mock_chain)

    server = create_mcp_server()
    res = await server.call_tool("web_fetch", {"url": "https://example.com"})
    assert not res.is_error
    assert "Hello" in res.content[0].text


@pytest.mark.anyio
async def test_tool_searchproxy_research(monkeypatch):
    """searchproxy_research generates deep multi-hop cited report."""
    mock_service = MagicMock()
    mock_service.research = AsyncMock(
        return_value=RetrieveResponse(
            query="AI architectures",
            answer="# Deep Research Report\n\nTransformers remain dominant [1].",
            citations=[
                Citation(
                    id=1,
                    url="https://arxiv.org/abs/1706.03762",
                    title="Attention Is All You Need",
                )
            ],
            sources=[],
            sources_fetched=5,
            sources_failed=0,
        )
    )
    monkeypatch.setattr("app.mcp_server.get_deep_research_service", lambda: mock_service)

    server = create_mcp_server()
    res = await server.call_tool(
        "searchproxy_research",
        {"query": "AI architectures", "fetch_top_k": 5},
    )
    assert not res.is_error
    text = res.content[0].text
    assert "# Deep Research Report" in text
    assert "Transformers remain dominant [1]." in text
    assert "https://arxiv.org/abs/1706.03762" in text



@pytest.mark.anyio
async def test_tool_empty_inputs(monkeypatch):
    """Empty queries or URLs return helpful error strings."""
    server = create_mcp_server()
    res1 = await server.call_tool("searchproxy_retrieve", {"query": "   "})
    assert "Error: query cannot be empty" in res1.content[0].text

    res2 = await server.call_tool("searchproxy_fetch", {"url": ""})
    assert "Error: url cannot be empty" in res2.content[0].text

    res3 = await server.call_tool("searchproxy_research", {"query": ""})
    assert "Error: query cannot be empty" in res3.content[0].text


# ---------------------------------------------------------------------------
# Transport & ASGI Route Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_mcp_messages_endpoint_session_validation(client):
    """POST /messages/ and /sse/messages/ require session_id query param and valid session."""
    # Missing session_id query param returns 400
    r1 = await client.post("/messages/", json={"jsonrpc": "2.0"})
    assert r1.status_code == 400

    r2 = await client.post("/sse/messages/", json={"jsonrpc": "2.0"})
    assert r2.status_code == 400

    # Non-existent session_id returns 404
    r3 = await client.post(
        "/messages/?session_id=00000000000000000000000000000000",
        json={"jsonrpc": "2.0"},
    )
    assert r3.status_code == 404


@pytest.mark.anyio
async def test_mcp_messages_redirects(client):
    """POST /messages and /sse/messages without trailing slashes redirect to trailing slash."""
    r1 = await client.post("/messages", follow_redirects=False)
    assert r1.status_code == 307

    r2 = await client.post("/sse/messages", follow_redirects=False)
    assert r2.status_code == 307


# ---------------------------------------------------------------------------
# End-to-End MCP Protocol Handshake & Tool Call Test
# ---------------------------------------------------------------------------

def _get_free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.mark.anyio
async def test_mcp_sse_client_end_to_end(monkeypatch):
    """Test full MCP handshake: SSE connect -> init -> list_tools -> call_tool."""
    mock_chain = MagicMock()
    mock_chain.execute = AsyncMock(
        return_value=FetchResult(
            success=True,
            url="https://mcp-test.com",
            title="MCP Test Page",
            markdown="Successfully fetched via MCP!",
        )
    )
    monkeypatch.setattr("app.mcp_server.get_fetch_chain", lambda: mock_chain)

    port = _get_free_port()
    config = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    # Wait for server to start
    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    try:
        url = f"http://127.0.0.1:{port}/sse"
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                # 1. Initialize
                init_result = await session.initialize()
                assert init_result.server_info.name == "SearchProxy"

                # 2. List tools
                tools_result = await session.list_tools()
                names = [t.name for t in tools_result.tools]
                assert "searchproxy_fetch" in names
                assert "searchproxy_retrieve" in names
                assert "searchproxy_research" in names

                # 3. Call tool
                call_result = await session.call_tool(
                    "searchproxy_fetch",
                    {"url": "https://mcp-test.com"},
                )
                assert not call_result.is_error
                assert "Successfully fetched via MCP!" in call_result.content[0].text
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)


@pytest.mark.anyio
async def test_tool_searchproxy_retrieve_string_domains(monkeypatch):
    """searchproxy_retrieve accepts include_domains as a comma-separated string."""
    mock_service = MagicMock()
    mock_service.retrieve = AsyncMock(
        return_value=RetrieveResponse(
            query="test",
            answer="Found results",
            citations=[],
            sources=[],
            sources_fetched=1,
            sources_failed=0,
        )
    )
    monkeypatch.setattr("app.mcp_server.get_retrieve_service", lambda: mock_service)

    server = create_mcp_server()
    res = await server.call_tool(
        "searchproxy_retrieve",
        {"query": "test", "include_domains": "docs.python.org, pypi.org"},
    )
    assert not res.is_error
    mock_service.retrieve.assert_awaited_once()
    kwargs = mock_service.retrieve.await_args.kwargs
    assert kwargs["include_domains"] == ["docs.python.org", "pypi.org"]


@pytest.mark.anyio
async def test_tool_exception_returns_error_message(monkeypatch):
    """Unexpected exceptions in tools return clear error string instead of crashing."""
    mock_service = MagicMock()
    mock_service.retrieve = AsyncMock(side_effect=RuntimeError("SearXNG upstream timeout"))
    monkeypatch.setattr("app.mcp_server.get_retrieve_service", lambda: mock_service)

    server = create_mcp_server()
    res = await server.call_tool(
        "searchproxy_retrieve",
        {"query": "crash test"},
    )
    assert not res.is_error
    assert "Error retrieving 'crash test': SearXNG upstream timeout" in res.content[0].text


@pytest.mark.anyio
async def test_mcp_query_param_auth_handshake(monkeypatch):
    """When auth is required, SSE connects and executes tools using ?api_key= query param."""
    import app.config as config
    original_auth = config.settings.SEARCHPROXY_REQUIRE_AUTH
    original_key = config.settings.SEARCHPROXY_API_KEY
    config.settings.SEARCHPROXY_REQUIRE_AUTH = True
    config.settings.SEARCHPROXY_API_KEY = "mcp-test-secret"

    mock_chain = MagicMock()
    mock_chain.execute = AsyncMock(
        return_value=FetchResult(
            success=True,
            url="https://auth-test.com",
            title="Auth Page",
            markdown="Authorized content",
        )
    )
    monkeypatch.setattr("app.mcp_server.get_fetch_chain", lambda: mock_chain)

    port = _get_free_port()
    config_uv = uvicorn.Config(
        fastapi_app,
        host="127.0.0.1",
        port=port,
        log_level="error",
    )
    server = uvicorn.Server(config_uv)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()

    for _ in range(50):
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.1):
                break
        except OSError:
            time.sleep(0.05)

    try:
        url = f"http://127.0.0.1:{port}/sse?api_key=mcp-test-secret"
        async with sse_client(url) as (read_stream, write_stream):
            async with ClientSession(read_stream, write_stream) as session:
                init_res = await session.initialize()
                assert init_res.server_info.name == "SearchProxy"

                call_res = await session.call_tool(
                    "searchproxy_fetch",
                    {"url": "https://auth-test.com"},
                )
                assert not call_res.is_error
                assert "Authorized content" in call_res.content[0].text
    finally:
        server.should_exit = True
        thread.join(timeout=2.0)
        config.settings.SEARCHPROXY_REQUIRE_AUTH = original_auth
        config.settings.SEARCHPROXY_API_KEY = original_key
