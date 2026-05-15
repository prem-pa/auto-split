"""Provider abstraction for receipt parsing (Brick D).

The orchestrator (Brick F) only ever sees an ``ExpenseParser`` — concrete
choice of LLM (Gemini today, maybe Claude tomorrow) is hidden behind this
Protocol. ``GroupContext`` is the small bundle of facts the parser needs
to resolve names and pick a default currency.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from app.ai.schema import ParsedExpense


@dataclass(frozen=True, slots=True)
class GroupContext:
    """Facts the parser needs about the group the expense lives in.

    Attributes:
        payer_name: Display name of the user who took the photo / paid.
            Used so the parser can emit a ``"self"`` split for them.
        member_names: Display names of other connected group members the
            parser may resolve mentions ("Priya", "Cody") against.
        default_currency: ISO 4217 fallback when the receipt + transcript
            don't make currency obvious.
    """

    payer_name: str
    member_names: list[str]
    default_currency: str


class ParserError(RuntimeError):
    """Raised when the parser cannot produce a valid ``ParsedExpense``.

    Brick F catches this and surfaces a friendly error to the user.
    """


@runtime_checkable
class ExpenseParser(Protocol):
    """Anything that turns (image, optional transcript, context) → expense."""

    async def parse(
        self,
        image_bytes: bytes,
        transcript: str | None,
        context: GroupContext,
    ) -> ParsedExpense:
        """Parse a receipt into a structured expense.

        Raises:
            ParserError: If the underlying LLM returns invalid JSON twice
                in a row or its output fails schema validation.
        """
        ...
