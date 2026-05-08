"""Tests for OpenAPI spec structure and /metrics endpoint."""
from __future__ import annotations

import pytest
from httpx import ASGITransport, AsyncClient

from app.main import app

# Static routes always present (lifespan-independent).
# Dynamic routes (/v1/search, /compat/searxng/search, /compat/firecrawl/*)
# are registered during lifespan when LiteLLM is reachable, so we don't
# assert them in unit tests.
STATIC_EXPECTED_PATHS = {
    "/health",
    "/fetch",
    "/compat/perplexity",
    "/vane",
    "/metrics",
}


@pytest.mark.anyio
async def test_openapi_json_returns_200():
    """GET /openapi.json returns 200."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert isinstance(spec, dict)
        assert "paths" in spec


@pytest.mark.anyio
async def test_openapi_version_is_3_0_3():
    """OpenAPI spec version is 3.0.3 for max client compatibility."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/openapi.json")
        assert resp.status_code == 200
        spec = resp.json()
        assert spec.get("openapi") == "3.0.3", f"Expected 3.0.3, got {spec.get('openapi')}"


@pytest.mark.anyio
async def test_spec_contains_static_paths():
    """OpenAPI spec contains all statically-registered endpoint paths."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/openapi.json")
        spec = resp.json()
        paths = set(spec.get("paths", {}).keys())
        for expected in STATIC_EXPECTED_PATHS:
            assert expected in paths, f"Missing path: {expected}. Got: {sorted(paths)}"


@pytest.mark.anyio
async def test_spec_is_fully_dereferenced():
    """OpenAPI spec has no $ref keys (custom dereference handler strips them)."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/openapi.json")
        spec = resp.json()

        def _has_ref(obj):
            if isinstance(obj, dict):
                if "$ref" in obj:
                    return True
                return any(_has_ref(v) for v in obj.values())
            if isinstance(obj, list):
                return any(_has_ref(v) for v in obj)
            return False

        assert not _has_ref(spec), "Spec still contains $ref keys"


@pytest.mark.anyio
async def test_health_endpoint_in_spec():
    """/health GET operation has a documented 200 response."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/openapi.json")
        spec = resp.json()
        health_path = spec["paths"].get("/health", {})
        health_get = health_path.get("get", {})
        responses = health_get.get("responses", {})
        assert "200" in responses, "/health GET missing 200 response"


@pytest.mark.anyio
async def test_metrics_endpoint_returns_prometheus_format():
    """GET /metrics returns 200 with Prometheus-style text."""
    async with AsyncClient(
        transport=ASGITransport(app=app), base_url="http://test"
    ) as client:
        resp = await client.get("/metrics")
        assert resp.status_code == 200
        assert "text/plain" in resp.headers.get("content-type", "")
        body = resp.text
        # Should contain at least the HELP/TYPE lines for request counters
        assert (
            "searchproxy_requests_total" in body
            or "searchproxy_fetch_chain_tiers_total" in body
        )