import json
import logging
import time
import uuid
from typing import Annotated, AsyncIterator

from fastapi import APIRouter, Depends, Request, status
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.dependencies import get_retrieve_service, get_search_router
from app.schemas import MessageItem
from app.services.retrieve_service import RetrieveService
from app.services.search import SearchResponse, SearchRouter

log = logging.getLogger(__name__)
router = APIRouter(prefix="", tags=["search"])


class PerplexityQuery(BaseModel):
    """Request body for /compat/perplexity and /v1/search.

    Supports two shapes:

    - Simple (preferred): ``{"query": "..."}``
    - Open WebUI / Perplexity-compatible: ``{"messages": [{"role": "user", "content": "..."}]}``

    When ``messages`` is provided, the query is extracted from the **last**
    ``user`` message. All other Perplexity fields (``model``, ``stream``,
    ``return_related_questions``, ``search_recency_filter``) are accepted but
    ignored.
    """

    model_config = ConfigDict(
        json_schema_extra={
            "examples": [
                {"query": "Real Madrid 2025 season"},
                {
                    "messages": [
                        {"role": "user", "content": "What is the capital of Canada?"}
                    ]
                },
            ]
        }
    )

    query: str = Field(
        default="",
        description="Search query string. Mutually exclusive with ``messages`` — provide one or the other.",
    )
    max_results: int = Field(
        default=10, ge=1, le=100, description="Maximum results to return"
    )
    # —— Open WebUI / Perplexity compat fields (ignored) ——
    messages: list[MessageItem] = Field(
        default=[],
        description="OpenAI-style messages array. If provided, query is extracted from the last user message.",
    )
    model: str = Field(default="", description="Ignored — forwarded for Open WebUI compat.")
    stream: bool = Field(default=False, description="Ignored — forwarded for Open WebUI compat.")
    return_related_questions: bool = Field(
        default=False, description="Ignored — forwarded for Open WebUI compat."
    )
    search_recency_filter: str = Field(
        default="", description="Ignored — forwarded for Open WebUI compat."
    )

    @model_validator(mode="after")
    def _extract_query(self) -> "PerplexityQuery":
        if not self.query and self.messages:
            for msg in reversed(self.messages):
                role = getattr(msg, "role", None)
                content = getattr(msg, "content", None)
                if role == "user" and isinstance(content, str):
                    self.query = content.strip()
                    break
        if not self.query:
            raise ValueError("Either 'query' or 'messages' (with a user message) is required.")
        return self


@router.post(
    "/compat/perplexity",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="Compatibility search — returns snippets only",
    description="Compatibility endpoint for Open WebUI integration. Returns search snippets without fetching or synthesis. Hidden from OpenAPI spec — agents should use /v1/retrieve instead.",
    operation_id="search_perplexity_compat",
    include_in_schema=False,
)
async def compat_perplexity(
    body: PerplexityQuery,
    client: Annotated[SearchRouter, Depends(get_search_router)],
) -> SearchResponse:
    """Compatibility endpoint for Open WebUI / Perplexity clients.

    Returns search result snippets only (no fetch, no synthesis).
    Accepts either ``{"query": "..."}`` or a full Perplexity shape with
    ``messages[]`` (query auto-extracted from the last user message).
    """
    log.info(
        "/compat/perplexity relay query='%s' max_results=%d",
        body.query,
        body.max_results,
    )
    return await client.search(query=body.query, max_results=body.max_results)


@router.post(
    "/v1/search",
    response_model=SearchResponse,
    status_code=status.HTTP_200_OK,
    summary="OpenAI-compatible search alias",
    operation_id="search_v1",
    include_in_schema=False,
)
async def openai_search_alias(
    body: PerplexityQuery,
    client: Annotated[SearchRouter, Depends(get_search_router)],
) -> SearchResponse:
    """Alias for /compat/perplexity — same request and response shape.

    Provided for clients expecting an OpenAI-style ``/v1/search`` endpoint.
    """
    log.info(
        "/v1/search alias query='%s' max_results=%d",
        body.query,
        body.max_results,
    )
    return await client.search(query=body.query, max_results=body.max_results)


class ChatCompletionRequest(BaseModel):
    """Standard OpenAI/Perplexity-style chat completion request."""

    model: str = Field(default="sonar", description="Model name.")
    messages: list[MessageItem] = Field(default=[], description="Chat messages array.")
    stream: bool = Field(default=False, description="Enable SSE streaming.")
    max_tokens: int | None = Field(default=None, description="Max tokens.")
    temperature: float | None = Field(default=None, description="Temperature.")


async def _openai_stream_adapter(
    query: str,
    model: str,
    stream_iter: AsyncIterator[str],
) -> AsyncIterator[str]:
    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())
    citations: list[str] = []

    async for line in stream_iter:
        if line.startswith("event: token\ndata: "):
            token_json = line[len("event: token\ndata: ") :].rstrip("\r\n")
            try:
                token = json.loads(token_json)
                chunk = {
                    "id": cmpl_id,
                    "object": "chat.completion.chunk",
                    "created": created,
                    "model": model,
                    "choices": [
                        {
                            "index": 0,
                            "delta": {"content": token},
                            "finish_reason": None,
                        }
                    ],
                }
                yield f"data: {json.dumps(chunk)}\n\n"
            except Exception:
                pass
        elif line.startswith("event: source\ndata: "):
            try:
                src_json = line[len("event: source\ndata: ") :].rstrip("\r\n")
                src_data = json.loads(src_json)
                if "url" in src_data:
                    citations.append(src_data["url"])
            except Exception:
                pass
        elif line.startswith("event: done\ndata: "):
            chunk = {
                "id": cmpl_id,
                "object": "chat.completion.chunk",
                "created": created,
                "model": model,
                "choices": [
                    {
                        "index": 0,
                        "delta": {},
                        "finish_reason": "stop",
                    }
                ],
                "citations": citations,
            }
            yield f"data: {json.dumps(chunk)}\n\n"
            yield "data: [DONE]\n\n"


@router.post(
    "/compat/perplexity/chat/completions",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="Perplexity-compatible /chat/completions endpoint",
    operation_id="perplexity_chat_completions",
    include_in_schema=False,
)
@router.post(
    "/v1/chat/completions",
    response_model=None,
    status_code=status.HTTP_200_OK,
    summary="OpenAI-compatible /chat/completions alias",
    operation_id="openai_chat_completions",
    include_in_schema=False,
)
async def perplexity_chat_completions(
    body: ChatCompletionRequest,
    request: Request,
    service: Annotated[RetrieveService, Depends(get_retrieve_service)],
) -> JSONResponse | StreamingResponse:
    """1:1 Drop-in replacement for Perplexity API / Open WebUI perplexity_search driver.

    Routes queries through SearchProxy's /v1/retrieve pipeline and returns an
    OpenAI-shaped chat completion response with citations array.
    """
    # Extract query from messages
    query = ""
    for msg in reversed(body.messages):
        if getattr(msg, "role", None) == "user" and getattr(msg, "content", None):
            query = msg.content.strip()
            break

    if not query:
        query = "hello"

    if body.stream:
        stream_iter = service.retrieve_stream(query=query, request=request)
        return StreamingResponse(
            _openai_stream_adapter(query, body.model, stream_iter),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    res = await service.retrieve(query=query, request=request)
    cmpl_id = f"chatcmpl-{uuid.uuid4().hex[:12]}"
    created = int(time.time())

    return JSONResponse(
        content={
            "id": cmpl_id,
            "object": "chat.completion",
            "created": created,
            "model": body.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": res.answer,
                    },
                    "finish_reason": "stop",
                }
            ],
            "citations": [c.url for c in res.citations],
            "sources": [s.model_dump() for s in res.sources],
        }
    )