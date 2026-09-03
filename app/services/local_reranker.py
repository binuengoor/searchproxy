"""Local ONNX cross-encoder reranker using fastembed."""

from __future__ import annotations

import asyncio
import logging
import os
import threading
from typing import Any, TYPE_CHECKING

if TYPE_CHECKING:
    from app.config import Settings

log = logging.getLogger(__name__)


class LocalReranker:
    """Local ONNX cross-encoder reranker wrapping fastembed.TextCrossEncoder.

    Executes inference in a worker thread via asyncio.to_thread to avoid blocking
    the FastAPI event loop. Lazy-loads the ONNX model on first call.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._model: Any = None
        self._lock = threading.Lock()
        self._init_failed = False
        self._init_error: str | None = None

    def _ensure_model(self) -> None:
        if self._model is not None:
            return
        if self._init_failed:
            raise RuntimeError(f"Local reranker initialization previously failed: {self._init_error}")

        with self._lock:
            if self._model is not None:
                return
            try:
                from fastembed.rerank.cross_encoder import TextCrossEncoder

                cache_dir = self._settings.FASTEMBED_CACHE_PATH
                if cache_dir:
                    os.makedirs(cache_dir, exist_ok=True)

                log.info(
                    "Loading local ONNX reranker model '%s' (cache_dir=%s)...",
                    self._settings.LOCAL_RERANK_MODEL,
                    cache_dir,
                )
                self._model = TextCrossEncoder(
                    model_name=self._settings.LOCAL_RERANK_MODEL,
                    cache_dir=cache_dir,
                )
                log.info("Local ONNX reranker model loaded successfully")
            except Exception as exc:
                self._init_failed = True
                self._init_error = str(exc)
                log.error("Failed to load local ONNX reranker model: %s", exc)
                raise

    def _rerank_sync(self, query: str, documents: list[str]) -> list[float]:
        self._ensure_model()
        assert self._model is not None
        scores_gen = self._model.rerank(query, documents)
        return [float(s) for s in scores_gen]

    async def rerank(self, query: str, documents: list[str]) -> list[float]:
        """Rerank documents against query asynchronously."""
        if not documents:
            return []
        return await asyncio.to_thread(self._rerank_sync, query, documents)
