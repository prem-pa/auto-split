"""Provider factory (Brick D).

Swapping the LLM (e.g. Gemini → Claude) is a one-line change in this
module; everything else depends only on the ``ExpenseParser`` Protocol.
"""

from __future__ import annotations

from app.ai.parse import GeminiExpenseParser
from app.ai.provider import ExpenseParser


def default_parser() -> ExpenseParser:
    """Return the parser implementation we're using right now."""
    return GeminiExpenseParser()
