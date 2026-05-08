"""Structured JSON log formatter with correlation_id support.

When LOG_FORMAT=json, all logs are emitted as single-line JSON objects
with fields: timestamp, level, logger, correlation_id, message.

When LOG_FORMAT=text (default), logs use the traditional human-readable format.
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone


class JsonFormatter(logging.Formatter):
    """Emit each log record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        log_entry: dict = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }

        # Attach correlation_id if present (set by CorrelationIdMiddleware + filter)
        correlation_id = getattr(record, "correlation_id", None)
        if correlation_id:
            log_entry["correlation_id"] = correlation_id

        # Attach any extra fields from the log call
        for key in ("url", "tier", "source", "status_code", "error", "duration_ms"):
            value = getattr(record, key, None)
            if value is not None:
                log_entry[key] = value

        if record.exc_info and record.exc_text is None:
            record.exc_text = self.formatException(record.exc_info)
        if record.exc_text:
            log_entry["exception"] = record.exc_text

        return json.dumps(log_entry, default=str)


class CorrelationIdFilter(logging.Filter):
    """Logging filter that attaches correlation_id from the current request context.

    Uses a context variable to propagate the correlation ID from ASGI middleware
    to the logging subsystem without coupling loggers to Starlette request objects.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        from app.middleware.correlation import _current_correlation_id
        cid = _current_correlation_id.get("")
        if cid:
            record.correlation_id = cid  # type: ignore[attr-defined]
        return True