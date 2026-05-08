"""Shared schemas for OpenAPI documentation.

Models defined here have no runtime function — they exist purely to give
Pydantic+FastAPI enough shape to emit rich OpenAPI fields (typed arrays,
field-level descriptions, examples) that Pydantic v1/v2 otherwise collapse
when given ``dict[str, Any]``.

Import into request-body models or response-body models as needed.
"""

from __future__ import annotations

from pydantic import BaseModel, Field


class MessageItem(BaseModel):
    """One entry in an OpenAI-style ``messages`` array.

    Only the fields needed for query extraction are typed here.
    Extra keys (``name``, ``tool_calls``, etc.) are silently accepted.
    """

    role: str = Field(..., description="Message role: ``user``, ``assistant``, or ``system``.")
    content: str | None = Field(default="", description="Message text content. Null for assistant tool-call messages.")
