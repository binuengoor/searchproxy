"""Dedicated thread pool for CPU-bound content cleaning.

trafilatura and regex cleaning can block for hundreds of milliseconds on
large HTML pages. A dedicated pool prevents those tasks from starving the
default asyncio thread pool used by other libraries.
"""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

_executor: ThreadPoolExecutor | None = None


def init_executor(max_workers: int = 16) -> ThreadPoolExecutor:
    """Create the dedicated cleaning executor."""
    global _executor
    _executor = ThreadPoolExecutor(
        max_workers=max_workers,
        thread_name_prefix="clean_",
    )
    return _executor


def get_executor() -> ThreadPoolExecutor:
    """Return the shared cleaning executor.

    Auto-initializes with a default configuration if not already set,
    so tests and standalone usage don't need explicit lifecycle management.
    """
    global _executor
    if _executor is None:
        _executor = ThreadPoolExecutor(
            max_workers=16,
            thread_name_prefix="clean_",
        )
    return _executor


def shutdown_executor() -> None:
    """Shut down the executor gracefully."""
    global _executor
    if _executor is not None:
        _executor.shutdown(wait=True)
        _executor = None
