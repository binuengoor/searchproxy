"""Structured JSON extraction service — scrapes a URL and extracts schema-guided data via LLM."""
from __future__ import annotations

import json
import logging
import re
from typing import Any

import httpx
import jsonschema

from app.config import Settings
from app.schemas import ExtractResponse
from app.services.fetch_chain import FetchChain

log = logging.getLogger(__name__)

_DEFAULT_SYSTEM_PROMPT = """\
You are an expert structured data extraction engine.
Your task is to extract information from the provided web page content and return ONLY a
valid JSON object matching the requested schema.
Do not include any conversational commentary, explanations, or Markdown code fence formatting.
Output pure, valid JSON.
"""


class ExtractService:
    """Scrapes web content via FetchChain and extracts structured data using an LLM."""

    def __init__(
        self,
        fetch_chain: FetchChain,
        http_client: httpx.AsyncClient,
        settings: Settings,
    ) -> None:
        self._fetch_chain = fetch_chain
        self._client = http_client
        self._settings = settings

    async def extract(
        self,
        url: str,
        schema: dict[str, Any] | None = None,
        prompt: str | None = None,
        system_prompt: str | None = None,
    ) -> ExtractResponse:
        """Extract structured JSON from a target URL according to a JSON schema.

        Args:
            url: Target URL to scrape and analyze.
            schema: Optional JSON Schema dict specifying expected data structure.
            prompt: Optional user instructions guiding what to extract.
            system_prompt: Optional custom system instructions.

        Returns:
            ExtractResponse containing success flag, extracted JSON data, and target URL.
        """
        clean_url = url.strip()
        if not clean_url:
            return ExtractResponse(
                success=False,
                data=None,
                url=clean_url,
                error="URL must not be empty.",
            )

        if not (clean_url.startswith("http://") or clean_url.startswith("https://")):
            clean_url = f"https://{clean_url}"

        # 1. Fetch content through tiered fetch chain
        log.info("Extracting structured data from url='%s'", clean_url)
        fetch_result = await self._fetch_chain.execute(clean_url)
        if not fetch_result.success:
            return ExtractResponse(
                success=False,
                data=None,
                url=clean_url,
                error=fetch_result.error or "Failed to fetch content from URL.",
            )

        markdown = fetch_result.markdown.strip() if fetch_result.markdown else ""
        if not markdown:
            return ExtractResponse(
                success=False,
                data=None,
                url=clean_url,
                error="Fetched page contained no extractable content.",
            )

        # 2. Build prompt components
        effective_system = system_prompt or _DEFAULT_SYSTEM_PROMPT

        user_content_parts = [f"Target URL: {clean_url}\n"]
        if schema:
            user_content_parts.append(
                f"Target JSON Schema:\n{json.dumps(schema, indent=2)}\n"
            )
        if prompt:
            user_content_parts.append(f"Extraction Instructions:\n{prompt}\n")

        # Cap markdown content to avoid overflowing model context window
        max_chars = 35000
        content_snippet = markdown
        if len(content_snippet) > max_chars:
            content_snippet = content_snippet[:max_chars] + "\n\n[Content truncated...]"

        user_content_parts.append(f"Web Page Content:\n{content_snippet}")
        user_message = "\n".join(user_content_parts)

        # 3. Call LLM for extraction
        headers: dict[str, str] = {"Content-Type": "application/json"}
        if self._settings.LLM_API_KEY:
            headers["Authorization"] = f"Bearer {self._settings.LLM_API_KEY}"

        payload: dict[str, Any] = {
            "model": self._settings.LLM_CHAT_MODEL,
            "messages": [
                {"role": "system", "content": effective_system},
                {"role": "user", "content": user_message},
            ],
            "temperature": 0.1,
            "response_format": {"type": "json_object"},
        }

        try:
            resp = await self._client.post(
                self._settings.LLM_CHAT_URL,
                json=payload,
                headers=headers,
                timeout=httpx.Timeout(self._settings.SYNTHESIS_TIMEOUT, connect=10.0),
            )
            # If provider returns 400 because response_format is unsupported, retry without it
            if resp.status_code == 400 and "response_format" in payload:
                payload_no_rf = dict(payload)
                del payload_no_rf["response_format"]
                resp = await self._client.post(
                    self._settings.LLM_CHAT_URL,
                    json=payload_no_rf,
                    headers=headers,
                    timeout=httpx.Timeout(self._settings.SYNTHESIS_TIMEOUT, connect=10.0),
                )
            resp.raise_for_status()
            resp_data = resp.json()
            raw_content = resp_data["choices"][0]["message"]["content"]
        except Exception as exc:
            log.warning("LLM extraction call failed for %s: %s", clean_url, exc)
            return ExtractResponse(
                success=False,
                data=None,
                url=clean_url,
                error=f"LLM extraction failed: {exc}",
            )

        # 4. Clean and parse JSON response
        cleaned = (raw_content or "").strip()
        if cleaned.startswith("```"):
            cleaned = re.sub(r"^```(?:json)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
            cleaned = cleaned.strip()

        try:
            parsed_data = json.loads(cleaned)
        except json.JSONDecodeError as err:
            match = re.search(r"(\{.*\}|\[.*\])", cleaned, re.DOTALL)
            if match:
                try:
                    parsed_data = json.loads(match.group(1))
                except Exception:
                    return ExtractResponse(
                        success=False,
                        data=None,
                        url=clean_url,
                        error=f"Failed to parse LLM output as JSON: {err}",
                    )
            else:
                return ExtractResponse(
                    success=False,
                    data=None,
                    url=clean_url,
                    error=f"Failed to parse LLM output as JSON: {err}",
                )

        # 5. Schema validation if schema is provided
        if schema and isinstance(schema, dict):
            try:
                jsonschema.validate(instance=parsed_data, schema=schema)
            except jsonschema.ValidationError as val_err:
                log.warning("Extracted JSON schema validation failed: %s", val_err.message)
                return ExtractResponse(
                    success=False,
                    data=parsed_data,
                    url=clean_url,
                    error=f"Schema validation error: {val_err.message}",
                )
            except jsonschema.SchemaError as schema_err:
                log.warning("Invalid JSON schema provided: %s", schema_err.message)
                return ExtractResponse(
                    success=False,
                    data=parsed_data,
                    url=clean_url,
                    error=f"Invalid JSON Schema: {schema_err.message}",
                )

        return ExtractResponse(success=True, data=parsed_data, url=clean_url)
