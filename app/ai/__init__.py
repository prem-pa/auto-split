"""Brick D — AI parsing.

Public interface for other bricks to import:

    from app.ai import (
        ParsedExpense,
        Split,
        GroupContext,
        ExpenseParser,
        ParserError,
        default_parser,
        transcribe_voice,
        TranscriptionError,
    )
"""

from app.ai.factory import default_parser
from app.ai.provider import ExpenseParser, GroupContext, ParserError
from app.ai.schema import ParsedExpense, Split
from app.ai.transcribe import TranscriptionError, transcribe_voice

__all__ = [
    "ExpenseParser",
    "GroupContext",
    "ParsedExpense",
    "ParserError",
    "Split",
    "TranscriptionError",
    "default_parser",
    "transcribe_voice",
]
