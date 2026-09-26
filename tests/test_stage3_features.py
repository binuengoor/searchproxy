"""Unit tests for SearchProxy Stage 3 features:
1. Exa Instant Text Bypass (Sub-800ms Zero-Fetch Path)
2. Reciprocal Rank Fusion (RRF) Hybrid Search
3. Post-Synthesis Citation Verification & Hallucination Filter
4. In-Process pymupdf4llm PDF Parser
"""
from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

from app.config import Settings
from app.schemas import Citation, SourceChunk
from app.services.fetch_chain import FetchChain
from app.services.models import FetchResult
from app.services.retrieve_service import RetrieveService
from app.services.retrieve_steps import verify_citations_step
from app.services.search.models import SearchResponse, SearchResult
from app.services.search.providers.exa import ExaSearchProvider
from app.services.search.router import SearchRouter, reciprocal_rank_fusion

# ---------------------------------------------------------------------------
# Feature 2: Exa Instant Text Bypass Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_exa_provider_requests_full_text():
    """ExaSearchProvider requests contents={"text": True} and sets SearchResult.text."""
    settings = Settings(EXA_API_KEY="test-exa-key")
    mock_client = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "results": [
            {
                "title": "Exa Page",
                "url": "https://example.com/exa",
                "text": "This is the full extracted text of the webpage from Exa API.",
            }
        ]
    }
    mock_client.post.return_value = mock_resp

    provider = ExaSearchProvider(client=mock_client, settings=settings)
    results = await provider.search("test query", max_results=5)

    assert len(results) == 1
    assert results[0].url == "https://example.com/exa"
    assert results[0].text == "This is the full extracted text of the webpage from Exa API."
    assert "full extracted text" in results[0].snippet

    # Verify request body had contents={"text": True}
    call_args = mock_client.post.call_args
    assert call_args is not None
    body = call_args[1]["json"]
    assert body["contents"] == {"text": True}


@pytest.mark.anyio
async def test_retrieve_service_instant_text_bypass():
    """When candidates have text >= RETRIEVE_MIN_CONTENT_LENGTH, FetchChain is bypassed."""
    settings = Settings(
        RETRIEVE_MIN_CONTENT_LENGTH=50,
        RETRIEVE_MAX_CONTENT_PER_SOURCE=1000,
        RETRIEVE_PREFETCH_DURING_RERANK=True,
    )
    mock_search = AsyncMock()
    full_content = (
        "This is extensive content directly from Exa that exceeds fifty characters comfortably."
    )
    mock_search.search.return_value = SearchResponse(
        results=[
            SearchResult(
                title="Instant Doc",
                url="https://instant.com",
                snippet="Short snippet",
                text=full_content,
            ),
            SearchResult(
                title="Fetch Doc",
                url="https://needs-fetch.com",
                snippet="No full text",
                text=None,
            ),
        ]
    )
    mock_rerank = AsyncMock()
    mock_fetch = AsyncMock()
    mock_fetch.execute.return_value = FetchResult(
        success=True,
        url="https://needs-fetch.com",
        markdown="Fetched markdown content from network that is long enough. " * 5,
        title="Fetch Doc",
        source="crawl4ai",
    )
    mock_synthesis = AsyncMock()
    mock_synthesis.synthesize.return_value = (
        "Answer [1] [2]",
        [
            Citation(id=1, url="https://instant.com", title="Instant Doc"),
            Citation(id=2, url="https://needs-fetch.com", title="Fetch Doc"),
        ],
    )

    service = RetrieveService(
        search_client=mock_search,
        rerank_service=mock_rerank,
        fetch_chain=mock_fetch,
        synthesis_service=mock_synthesis,
        settings=settings,
    )

    resp = await service.retrieve(query="test", max_results=2, fetch_top_k=2)

    assert len(resp.sources) == 2
    # Verify instant source used search_instant tier and 0.0 fetch_time_ms
    instant_src = next(s for s in resp.sources if s.url == "https://instant.com")
    assert instant_src.fetch_tier == "search_instant"
    assert instant_src.fetch_time_ms == 0.0
    assert instant_src.content == full_content

    # FetchChain was only called for the second URL, not the instant one!
    assert mock_fetch.execute.await_count == 1
    call_url = mock_fetch.execute.call_args[0][0]
    assert call_url == "https://needs-fetch.com"


# ---------------------------------------------------------------------------
# Feature 3: RRF Hybrid Search Tests
# ---------------------------------------------------------------------------

def test_reciprocal_rank_fusion_math():
    """Verify RRF formula: RRF(d) = sum(1 / (60 + rank(d)))."""
    doc_a = SearchResult(title="A", url="https://a.com", snippet="Doc A")
    doc_b = SearchResult(title="B", url="https://b.com", snippet="Doc B")
    doc_c = SearchResult(title="C", url="https://c.com", snippet="Doc C")

    # List 1: [A, B] -> rank A=1, B=2
    # List 2: [B, C] -> rank B=1, C=2
    list1 = [doc_a, doc_b]
    list2 = [doc_b, doc_c]

    fused = reciprocal_rank_fusion([list1, list2], k=60, max_results=3)

    # Score A = 1 / (60 + 1) = 1 / 61 = 0.016393
    # Score B = 1 / (60 + 2) + 1 / (60 + 1) = 1/62 + 1/61 = 0.016129 + 0.016393 = 0.032522
    # Score C = 1 / (60 + 2) = 1 / 62 = 0.016129
    # B should rank first because it appeared in both lists!
    assert fused[0].url == "https://b.com"
    assert fused[1].url == "https://a.com"
    assert fused[2].url == "https://c.com"


@pytest.mark.anyio
async def test_search_router_hybrid():
    """SearchRouter.search with hybrid=True fuses results from two providers."""
    settings = Settings()
    mock_client = AsyncMock()

    class FakeProvider:
        def __init__(self, name: str, items: list[SearchResult]):
            self.name = name
            self.tier = 1
            self.is_available = True
            self._items = items

        async def search(self, **kwargs):
            return self._items

    prov_lex = FakeProvider("brave", [
        SearchResult(title="Lex 1", url="https://lex.com", snippet="Lexical"),
        SearchResult(title="Shared", url="https://shared.com", snippet="Shared snippet 1"),
    ])
    prov_sem = FakeProvider("exa", [
        SearchResult(
            title="Shared",
            url="https://shared.com",
            snippet="Shared snippet 2",
            text="Exa full text",
        ),
        SearchResult(title="Sem 1", url="https://sem.com", snippet="Semantic"),
    ])

    router = SearchRouter(
        client=mock_client,
        settings=settings,
        custom_providers=[prov_lex, prov_sem],
    )

    resp = await router.search("hybrid query", max_results=5, hybrid=True)
    assert len(resp.results) == 3
    # shared.com should be rank #1 because it appeared in both lexical and semantic!
    assert resp.results[0].url == "https://shared.com"
    # Merging preserves Exa text
    assert resp.results[0].text == "Exa full text"


# ---------------------------------------------------------------------------
# Feature 4: Citation Verification & Hallucination Filter Tests
# ---------------------------------------------------------------------------

def test_verify_citations_removes_out_of_bounds_and_hallucinations():
    """verify_citations_step filters out [N > len(sources)] and claims with no overlap."""
    sources = [
        SourceChunk(
            url="https://src1.com",
            title="Python Release",
            content="Python 3.13 introduces free-threaded CPython and a new JIT compiler.",
        ),
        SourceChunk(
            url="https://src2.com",
            title="French Cuisine",
            content="Classic French recipes rely heavily on butter, garlic, and fresh herbs.",
        ),
    ]

    answer = (
        "Python 3.13 includes a JIT compiler [1]. "
        "The universe is 14 billion years old [1]. "
        "Butter is essential in French cooking [2][99]. "
        "Aliens visited Earth [2]."
    )

    cleaned_answer, citations = verify_citations_step(answer, sources)

    # 1. [1] on JIT should remain
    assert "JIT compiler [1]" in cleaned_answer
    # 2. [1] on Universe has 0 overlap with Python source -> should be removed
    assert "14 billion years old [1]" not in cleaned_answer
    # 3. [2] on Butter should remain; [99] out of bounds -> removed
    assert "French cooking [2]" in cleaned_answer
    assert "[99]" not in cleaned_answer
    # 4. [2] on Aliens has 0 overlap with French cuisine -> removed
    assert "visited Earth [2]" not in cleaned_answer

    # 5. Citations list should only include the verified sources (1 and 2)
    assert len(citations) == 2
    assert citations[0].id == 1
    assert citations[1].id == 2


def test_verify_citations_filters_unused_sources():
    """Only sources actually cited in the text are returned in the citations list."""
    sources = [
        SourceChunk(url="https://src1.com", title="A", content="Quantum computers use qubits."),
        SourceChunk(url="https://src2.com", title="B", content="Classical computers use bits."),
        SourceChunk(
            url="https://src3.com",
            title="C",
            content="Superconductors have zero resistance.",
        ),
    ]

    # Only source 1 and 3 are cited
    answer = "Qubits are used in quantum computers [1]. Superconductors have zero resistance [3]."

    cleaned_answer, citations = verify_citations_step(answer, sources)

    assert len(citations) == 2
    cited_urls = [c.url for c in citations]
    assert "https://src1.com" in cited_urls
    assert "https://src3.com" in cited_urls
    assert "https://src2.com" not in cited_urls


# ---------------------------------------------------------------------------
# Feature 5: In-Process pymupdf4llm PDF Parser Tests
# ---------------------------------------------------------------------------

@pytest.mark.anyio
async def test_pymupdf4llm_pdf_extraction():
    """Valid PDF content is parsed in-process by pymupdf4llm and returns source='pymupdf4llm'."""
    import pymupdf

    # Generate a real in-memory PDF with sufficient text (>= 50 chars)
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text(
        (50, 72),
        "This is a high quality PDF document containing research data and experimental results "
        "that exceeds fifty characters for testing.",
    )
    pdf_bytes = doc.tobytes()
    doc.close()

    settings = Settings(TIKA_URL="http://tika:9998/tika")
    mock_client = AsyncMock()

    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = pdf_bytes
    mock_client.get.return_value = mock_resp

    chain = FetchChain(client=mock_client, settings=settings)
    chain._tika.parse_bytes = AsyncMock()

    result = await chain.execute("https://example.com/paper.pdf")

    assert result.success is True
    assert result.source == "pymupdf4llm"
    assert "research data and experimental results" in result.markdown
    # Tika fallback was NOT invoked because pymupdf4llm succeeded!
    chain._tika.parse_bytes.assert_not_called()


@pytest.mark.anyio
async def test_pymupdf4llm_fallback_to_tika_on_short_or_corrupt_content():
    """If PDF extraction produces < 50 chars or is corrupt, cleanly fall back to Tika."""
    settings = Settings(TIKA_URL="http://tika:9998/tika")
    mock_client = AsyncMock()

    # Corrupt or short text bytes (< 50 chars)
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.content = b"Short corrupt bytes"
    mock_client.get.return_value = mock_resp

    chain = FetchChain(client=mock_client, settings=settings)
    chain._tika.is_configured = MagicMock(return_value=True)
    chain._tika.parse_bytes = AsyncMock(
        return_value=FetchResult(
            success=True,
            url="https://example.com/scan.pdf",
            markdown="Extracted OCR text from Tika server",
            source="tika",
        )
    )

    result = await chain.execute("https://example.com/scan.pdf")

    assert result.success is True
    assert result.source == "tika"
    assert result.markdown == "Extracted OCR text from Tika server"
    chain._tika.parse_bytes.assert_awaited_once()


# ---------------------------------------------------------------------------
# Additional Deep Regression Tests for Edge Cases
# ---------------------------------------------------------------------------

def test_verify_citations_preserves_code_blocks_and_indentation():
    """Code blocks containing array indexing arr[0], arr[1] and indented lists are protected."""
    sources = [
        SourceChunk(
            url="https://python.org",
            title="Python Tutorial",
            content="Python lists support indexing and slicing operations.",
        ),
    ]

    answer = (
        "# Code Example\n\n"
        "Here is sample code:\n"
        "```python\n"
        "x = arr[0]\n"
        "y = arr[1]\n"
        "```\n\n"
        "Python lists support indexing [1].\n"
        "- Root item\n"
        "  - Nested item with [1]\n"
        "Access via `arr[0]` or `arr[1]`."
    )

    cleaned_answer, citations = verify_citations_step(answer, sources)

    # 1. Code block must NOT have arr[0] or arr[1] stripped
    assert "x = arr[0]" in cleaned_answer
    assert "y = arr[1]" in cleaned_answer
    # 2. Inline code `arr[0]` must remain intact
    assert "`arr[0]`" in cleaned_answer
    # 3. Indented list item must preserve its 2-space indentation
    assert "  - Nested item with" in cleaned_answer
    # 4. Valid citation [1] must remain
    assert "indexing [1]" in cleaned_answer
    assert len(citations) == 1
    assert citations[0].url == "https://python.org"


def test_verify_citations_cleans_dangling_commas():
    """Clean dangling punctuation like ', .' when a comma-separated citation is removed."""
    sources = [
        SourceChunk(
            url="https://qubits.com", title="Qubits", content="Quantum computers use qubits."
        ),
        SourceChunk(
            url="https://other.com", title="Other", content="Completely unrelated content."
        ),
    ]
    answer = "Quantum computers use qubits [1], [2]."
    cleaned_answer, citations = verify_citations_step(answer, sources)
    # [2] is hallucinated and removed, should not leave ', .'
    assert ",." not in cleaned_answer
    assert ", ." not in cleaned_answer
    assert cleaned_answer.endswith("qubits [1].")
    assert len(citations) == 1


def test_reciprocal_rank_fusion_preserves_text_regardless_of_order():
    """RRF preserves candidate full text regardless of whether Exa is first or second list."""
    doc_exa = SearchResult(
        title="Doc",
        url="https://ex.com/page",
        snippet="Short Exa snippet",
        text="Full text from Exa that should never be lost.",
    )
    doc_lex = SearchResult(
        title="Doc",
        url="https://ex.com/page",
        snippet="Much longer lexical snippet that exceeds the Exa snippet length by far",
        text=None,
    )

    # Order 1: Exa first, Lexical second
    fused1 = reciprocal_rank_fusion([[doc_exa], [doc_lex]])
    assert len(fused1) == 1
    assert fused1[0].text == "Full text from Exa that should never be lost."

    # Order 2: Lexical first, Exa second
    fused2 = reciprocal_rank_fusion([[doc_lex], [doc_exa]])
    assert len(fused2) == 1
    assert fused2[0].text == "Full text from Exa that should never be lost."


@pytest.mark.anyio
async def test_search_router_hybrid_fallback_with_tavily():
    """If Brave/SearXNG are unavailable, hybrid search pairs Exa with Tavily."""
    settings = Settings()
    mock_client = AsyncMock()

    class FakeProvider:
        def __init__(self, name: str):
            self.name = name
            self.tier = 1
            self.is_available = True

        async def search(self, **kwargs):
            return [
                SearchResult(
                    title=self.name,
                    url=f"https://{self.name}.com",
                    snippet=f"From {self.name}",
                )
            ]

    prov_exa = FakeProvider("exa")
    prov_tavily = FakeProvider("tavily")

    router = SearchRouter(
        client=mock_client,
        settings=settings,
        custom_providers=[prov_exa, prov_tavily],
    )

    resp = await router.search("test query", max_results=5, hybrid=True)
    assert len(resp.results) == 2
    urls = [r.url for r in resp.results]
    assert "https://exa.com" in urls
    assert "https://tavily.com" in urls


@pytest.mark.anyio
async def test_deep_research_hop2_relevance_scores_preserved():
    """Verify that Hop 2 candidates receive their correct relevance scores from score_map2."""
    from app.services.deep_research_service import DeepResearchService
    from app.services.rerank_service import RerankResult

    settings = Settings(
        LLM_CHAT_URL="https://api.openai.com/v1/chat/completions",
        LLM_CHAT_MODEL="gpt-4o-mini",
        LLM_API_KEY="test-key",
        RETRIEVE_MIN_CONTENT_LENGTH=20,
    )
    mock_search = AsyncMock()
    mock_rerank = AsyncMock()
    mock_fetch = AsyncMock()
    mock_synthesis = AsyncMock()
    mock_http = AsyncMock()

    service = DeepResearchService(
        search_client=mock_search,
        rerank_service=mock_rerank,
        fetch_chain=mock_fetch,
        synthesis_service=mock_synthesis,
        settings=settings,
        http_client=mock_http,
    )

    # 1. LLM mocks: decompose -> gap -> synthesis
    mock_http.post.side_effect = [
        # decompose
        httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": '["sub 1"]'}}]},
            request=httpx.Request("POST", "https://api.openai.com"),
        ),
        # gap analysis
        httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": '["gap 1"]'}}]},
            request=httpx.Request("POST", "https://api.openai.com"),
        ),
        # synthesis report
        httpx.Response(
            status_code=200,
            json={"choices": [{"message": {"content": "Report with facts [1] [2]."}}]},
            request=httpx.Request("POST", "https://api.openai.com"),
        ),
    ]

    # 2. Search mocks: Hop 1 gives 3 docs, Hop 2 gives 2 docs (so len > top_k triggers rerank)
    mock_search.search.side_effect = [
        SearchResponse(
            results=[
                SearchResult(
                    title="Doc 1",
                    url="https://doc1.com",
                    snippet="Doc 1",
                    text="Hop 1 content facts for quantum.",
                ),
                SearchResult(title="Doc 1B", url="https://doc1b.com", snippet="Doc 1B"),
                SearchResult(title="Doc 1C", url="https://doc1c.com", snippet="Doc 1C"),
            ]
        ),
        SearchResponse(results=[]),
        SearchResponse(
            results=[
                SearchResult(
                    title="Doc 2",
                    url="https://doc2.com",
                    snippet="Doc 2",
                    text="Hop 2 content facts for quantum.",
                ),
                SearchResult(title="Doc 2B", url="https://doc2b.com", snippet="Doc 2B"),
            ]
        ),
    ]

    # 3. Rerank returns distinct scores for Hop 1 and Hop 2
    mock_rerank.rerank.side_effect = [
        [
            RerankResult(index=0, relevance_score=0.91, text="Doc 1"),
            RerankResult(index=1, relevance_score=0.50, text="Doc 1B"),
        ],  # Hop 1
        [
            RerankResult(index=0, relevance_score=0.84, text="Doc 2"),
            RerankResult(index=1, relevance_score=0.40, text="Doc 2B"),
        ],  # Hop 2
    ]

    result = await service.research(query="quantum", fetch_top_k=2)
    assert len(result.sources) == 2
    # Verify Hop 1 source kept score 0.91
    hop1_src = next(s for s in result.sources if s.url == "https://doc1.com")
    assert hop1_src.relevance_score == 0.91
    # Verify Hop 2 source kept score 0.84 (NOT None!)
    hop2_src = next(s for s in result.sources if s.url == "https://doc2.com")
    assert hop2_src.relevance_score == 0.84

