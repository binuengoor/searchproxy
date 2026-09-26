"""Native Model Context Protocol (MCP) server integration for SearchProxy.

Exposes SearchProxy tools to MCP-compatible AI agents (Claude Code, Cursor, Open WebUI):
- searchproxy_retrieve (web_search)
- searchproxy_fetch (web_fetch)
- searchproxy_research (deep_research)

Supports SSE transport mounted at /sse and message handling at /messages/ (and /sse/messages).
"""
from __future__ import annotations

import asyncio
import json
import logging
import threading
import uuid

import anyio
from mcp.server.mcpserver import MCPServer
from mcp.server.sse import SseServerTransport
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.routing import Mount, Route
from starlette.types import Receive, Scope, Send

from app.dependencies import (
    get_deep_research_service,
    get_fetch_chain,
    get_retrieve_service,
)

log = logging.getLogger(__name__)

_server_lock = threading.RLock()
_mcp_server: MCPServer | None = None
_mcp_transport: SseServerTransport | None = None
_mcp_sse_app: Starlette | None = None


def _normalize_domains(domains: list[str] | str | None) -> list[str]:
    """Normalize domain filter inputs from string or list to clean list of domains."""
    if not domains:
        return []
    if isinstance(domains, str):
        return [d.strip() for d in domains.split(",") if d.strip()]
    return [str(d).strip() for d in domains if str(d).strip()]


def create_mcp_server() -> MCPServer:
    """Create and configure the SearchProxy MCPServer with registered tools."""
    server = MCPServer("SearchProxy", version="0.1.0")

    @server.tool(
        name="searchproxy_retrieve",
        description=(
            "Search the web, rerank results, crawl top sources, and return a cited synthesized "
            "answer or raw ranked chunks."
        ),
    )
    async def searchproxy_retrieve(
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        synthesize: bool = True,
        include_domains: list[str] | str | None = None,
        exclude_domains: list[str] | str | None = None,
        freshness: str | None = None,
    ) -> str:
        """Search and retrieve cited answers or content chunks."""
        clean_query = query.strip()
        if not clean_query:
            return "Error: query cannot be empty."

        try:
            service = get_retrieve_service()
            resp = await service.retrieve(
                query=clean_query,
                max_results=max_results,
                fetch_top_k=fetch_top_k,
                synthesize=synthesize,
                include_domains=_normalize_domains(include_domains),
                exclude_domains=_normalize_domains(exclude_domains),
                freshness=freshness,
            )
        except Exception as exc:
            log.warning("searchproxy_retrieve failed for '%s': %s", clean_query, exc)
            return f"Error retrieving '{clean_query}': {exc}"

        if synthesize:
            output_parts = [resp.answer]
            if resp.citations:
                output_parts.append("\n\n### Sources")
                for c in resp.citations:
                    title_suffix = f" - {c.title}" if c.title else ""
                    output_parts.append(f"[{c.id}] {c.url}{title_suffix}")
            return "\n".join(output_parts)
        else:
            if not resp.sources:
                return f"No sources found or fetched for query: {clean_query}"
            output_parts = [f"Found and fetched {len(resp.sources)} sources for '{clean_query}':\n"]
            for i, s in enumerate(resp.sources, 1):
                output_parts.append(
                    f"### [{i}] {s.title or s.url}\nURL: {s.url}\n\n{s.content}\n\n---"
                )
            return "\n".join(output_parts)

    @server.tool(
        name="web_search",
        description=(
            "Search the web, rerank results, crawl top sources, and return cited synthesis "
            "or raw ranked chunks (alias for searchproxy_retrieve)."
        ),
    )
    async def web_search(
        query: str,
        max_results: int = 10,
        fetch_top_k: int = 5,
        synthesize: bool = True,
        include_domains: list[str] | str | None = None,
        exclude_domains: list[str] | str | None = None,
        freshness: str | None = None,
    ) -> str:
        """Web search alias."""
        return await searchproxy_retrieve(
            query=query,
            max_results=max_results,
            fetch_top_k=fetch_top_k,
            synthesize=synthesize,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            freshness=freshness,
        )

    @server.tool(
        name="searchproxy_fetch",
        description=(
            "Fetch clean markdown from a single URL via SearchProxy's multi-tier fetch chain "
            "(Crawl4AI, Jina, Byparr, Apache Tika for PDFs/docs, anti-bot firebreak)."
        ),
    )
    async def searchproxy_fetch(url: str) -> str:
        """Fetch clean markdown content from a specific URL."""
        clean_url = url.strip()
        if not clean_url:
            return "Error: url cannot be empty."

        try:
            chain = get_fetch_chain()
            res = await chain.execute(clean_url)
        except Exception as exc:
            log.warning("searchproxy_fetch failed for '%s': %s", clean_url, exc)
            return f"Error fetching {clean_url}: {exc}"

        if not res.success:
            return f"Error fetching {clean_url}: {res.error or 'Failed to fetch content'}"

        parts = []
        if res.title:
            parts.append(f"# {res.title}\nURL: {clean_url}\n")
        else:
            parts.append(f"URL: {clean_url}\n")
        if res.description:
            parts.append(f"> {res.description}\n")
        parts.append(res.markdown)
        return "\n".join(parts)

    @server.tool(
        name="web_fetch",
        description=(
            "Fetch clean markdown from a single URL via FetchChain "
            "(alias for searchproxy_fetch)."
        ),
    )
    async def web_fetch(url: str) -> str:
        """Web fetch alias."""
        return await searchproxy_fetch(url=url)

    @server.tool(
        name="searchproxy_research",
        description=(
            "Multi-hop deep research with autonomous query decomposition, parallel searches "
            "across multiple angles, neural reranking, and an extensive cited report or dossier."
        ),
    )
    async def searchproxy_research(
        query: str,
        fetch_top_k: int = 8,
        include_domains: list[str] | str | None = None,
        exclude_domains: list[str] | str | None = None,
        format: str = "markdown",
    ) -> str:
        """Execute deep multi-hop research and produce a comprehensive cited report or dossier."""
        clean_query = query.strip()
        if not clean_query:
            return "Error: query cannot be empty."

        try:
            service = get_deep_research_service()
            resp = await service.research(
                query=clean_query,
                fetch_top_k=fetch_top_k,
                include_domains=_normalize_domains(include_domains),
                exclude_domains=_normalize_domains(exclude_domains),
                format=format,
            )
        except Exception as exc:
            log.warning("searchproxy_research failed for '%s': %s", clean_query, exc)
            return f"Error during deep research for '{clean_query}': {exc}"

        output_parts = [resp.answer]
        if resp.citations and format != "dossier":
            output_parts.append("\n\n### Sources")
            for c in resp.citations:
                title_suffix = f" - {c.title}" if c.title else ""
                output_parts.append(f"[{c.id}] {c.url}{title_suffix}")
        return "\n".join(output_parts)

    @server.tool(
        name="deep_research",
        description=(
            "Multi-hop deep research with query decomposition and comprehensive cited report "
            "(alias for searchproxy_research)."
        ),
    )
    async def deep_research(
        query: str,
        fetch_top_k: int = 8,
        include_domains: list[str] | str | None = None,
        exclude_domains: list[str] | str | None = None,
        format: str = "markdown",
    ) -> str:
        """Deep research alias."""
        return await searchproxy_research(
            query=query,
            fetch_top_k=fetch_top_k,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            format=format,
        )

    # ── MCP Resources ────────────────────────────────────────────────────────

    @server.resource(
        "searchproxy://research/recent",
        name="recent_research",
        description="List recent deep research dossiers generated by SearchProxy.",
    )
    async def get_recent_research() -> str:
        """Return a JSON list of recent research dossiers with metadata and links."""
        try:
            service = get_deep_research_service()
            entries = await service.get_recent_research(limit=20)
            return json.dumps({"research": entries, "count": len(entries)}, indent=2)
        except Exception as exc:
            log.warning("Failed to fetch recent research: %s", exc)
            return json.dumps({"error": str(exc), "research": [], "count": 0})

    @server.resource(
        "searchproxy://research/{id}",
        name="research_dossier",
        description="Retrieve a full research dossier or markdown report by its unique ID.",
    )
    async def get_research_dossier(id: str) -> str:
        """Return the dossier or report content for the specified research ID."""
        clean_id = id.strip()
        if not clean_id:
            return "Error: research id cannot be empty."

        try:
            service = get_deep_research_service()
            entry = await service.get_research_by_id(clean_id)
            if not entry:
                return f"Error: Research dossier with ID '{clean_id}' not found."
            return (
                entry.get("dossier")
                or entry.get("summary")
                or json.dumps(entry, indent=2)
            )
        except Exception as exc:
            log.warning("Failed to fetch research dossier '%s': %s", clean_id, exc)
            return f"Error retrieving research dossier '{clean_id}': {exc}"

    # ── MCP Prompts ──────────────────────────────────────────────────────────

    @server.prompt(
        name="technical_bug_investigation",
        description=(
            "Guide an autonomous deep technical bug root cause investigation, error code lookup, "
            "GitHub issue search, patch notes analysis, and architectural trace."
        ),
    )
    def technical_bug_investigation(query: str, logs_or_errors: str = "") -> str:
        """Prompt to guide deep technical bug root cause investigation."""
        prompt_lines = [
            "Perform an exhaustive technical root-cause investigation for the following bug/issue:",
            f"**Issue Description / Query:** {query}",
        ]
        if logs_or_errors.strip():
            prompt_lines.append(
                f"\n**Observed Errors & Logs:**\n```\n{logs_or_errors.strip()}\n```"
            )
        prompt_lines.extend(
            [
                "\n**Investigation Protocol:**",
                (
                    "1. **Information Retrieval & Error Code Lookup**: Formulate targeted search queries "
                    "for error codes/signatures, exact exception messages, GitHub issue tracker discussions, "
                    "and upstream changelogs/patch notes for recent regressions."
                ),
                (
                    "2. **Mechanism Analysis**: Trace component boundaries, race conditions, "
                    "edge-case invariants, and memory/concurrency mechanics that could produce "
                    "this behavior."
                ),
                (
                    "3. **Remediation & Patch Verification**: Deliver verified reproduction steps, "
                    "concrete patch code, patch notes analysis, and regression tests to guarantee long-term stability."
                ),
            ]
        )
        return "\n".join(prompt_lines)

    @server.prompt(
        name="market_competitive_analysis",
        description=(
            "Guide an executive competitor landscape, feature matrix, pricing, and "
            "positioning analysis."
        ),
    )
    def market_competitive_analysis(topic: str, target_competitors: str = "") -> str:
        """Prompt to guide executive market and competitive intelligence analysis."""
        prompt_lines = [
            "Perform a comprehensive market and competitive intelligence analysis for:",
            f"**Market Domain / Topic:** {topic}",
        ]
        if target_competitors.strip():
            prompt_lines.append(f"\n**Target Competitors:** {target_competitors.strip()}")
        prompt_lines.extend(
            [
                "\n**Analysis Protocol:**",
                (
                    "1. **Market Landscape**: Identify key incumbent players, emerging "
                    "challengers, and product differentiators."
                ),
                (
                    "2. **Comparative Matrix**: Synthesize feature parity, technical "
                    "architecture, pricing tiers, and licensing models."
                ),
                (
                    "3. **Moats & Strategic Recommendations**: Highlight vulnerabilities, "
                    "defensive moats, market gaps, and actionable positioning recommendations."
                ),
            ]
        )
        return "\n".join(prompt_lines)

    @server.prompt(
        name="academic_literature_review",
        description=(
            "Guide a comprehensive state-of-the-art academic review, methodology synthesis, "
            "citations, and future directions."
        ),
    )
    def academic_literature_review(topic: str, focus_areas: str = "") -> str:
        """Prompt to guide comprehensive state-of-the-art academic literature review."""
        prompt_lines = [
            "Conduct a rigorous academic literature review and state-of-the-art synthesis on:",
            f"**Research Topic:** {topic}",
        ]
        if focus_areas.strip():
            prompt_lines.append(f"\n**Specific Focus Areas:** {focus_areas.strip()}")
        prompt_lines.extend(
            [
                "\n**Review Protocol:**",
                (
                    "1. **Seminal & Modern Work**: Survey foundational breakthroughs and recent "
                    "benchmark-setting publications across arXiv, conferences, and journals."
                ),
                (
                    "2. **Methodology & Architecture**: Contrast algorithmic paradigms, "
                    "mathematical formulations, and empirical evaluation metrics."
                ),
                (
                    "3. **Open Challenges**: Pinpoint unresolved research limitations, failure "
                    "modes, and high-impact trajectories for future investigation."
                ),
            ]
        )
        return "\n".join(prompt_lines)

    return server


def get_mcp_server() -> MCPServer:
    """Return the shared MCPServer singleton (thread-safe lazy init)."""
    global _mcp_server
    if _mcp_server is None:
        with _server_lock:
            if _mcp_server is None:
                _mcp_server = create_mcp_server()
    return _mcp_server


def get_mcp_transport() -> SseServerTransport:
    """Return the shared SseServerTransport singleton."""
    global _mcp_transport
    if _mcp_transport is None:
        with _server_lock:
            if _mcp_transport is None:
                _mcp_transport = SseServerTransport(
                    "/messages/",
                    security_settings=TransportSecuritySettings(
                        enable_dns_rebinding_protection=False
                    ),
                )
    return _mcp_transport


def is_valid_mcp_session(session_id_str: str) -> bool:
    """Check if session_id_str corresponds to an active, authenticated MCP SSE session."""
    try:
        session_uuid = uuid.UUID(hex=session_id_str)
    except (ValueError, TypeError):
        return False
    transport = get_mcp_transport()
    return session_uuid in transport._read_stream_writers


class MCPSSEHandler:
    """Raw ASGI endpoint for GET /sse streaming without double http.response.start."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        server = get_mcp_server()
        transport = get_mcp_transport()
        try:
            async with transport.connect_sse(scope, receive, send) as streams:
                await server._lowlevel_server.run(
                    streams[0],
                    streams[1],
                    server._lowlevel_server.create_initialization_options(),
                )
        except (anyio.get_cancelled_exc_class(), asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("MCP SSE connection terminated: %s", exc)


class MCPPostHandler:
    """Raw ASGI endpoint for POST /messages/ and /sse/messages/."""

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        transport = get_mcp_transport()
        try:
            await transport.handle_post_message(scope, receive, send)
        except (anyio.get_cancelled_exc_class(), asyncio.CancelledError):
            pass
        except Exception as exc:
            log.debug("MCP message POST terminated: %s", exc)


def get_mcp_sse_app() -> Starlette:
    """Return the Starlette ASGI application hosting the MCP SSE endpoints."""
    global _mcp_sse_app
    if _mcp_sse_app is None:
        with _server_lock:
            if _mcp_sse_app is None:
                _mcp_sse_app = Starlette(
                    routes=[
                        Route("/sse", endpoint=MCPSSEHandler(), methods=["GET", "HEAD"]),
                        Mount("/messages", app=MCPPostHandler()),
                        Mount("/sse/messages", app=MCPPostHandler()),
                    ]
                )
    return _mcp_sse_app
