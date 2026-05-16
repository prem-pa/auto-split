"""Pydantic models for AI-parsed expense data (Brick D).

These types are the contract between the parser (Gemini) and the rest of
the system. They are also the LLM's structured-output schema — we ship
``ParsedExpense.model_json_schema()`` to Gemini so it returns JSON we can
``model_validate_json`` straight back.

Design notes
------------
* ``Decimal`` for ``amount`` so cents stay exact. Pydantic serialises it as
  a number when ``mode="json"`` — that's what we want on the wire.
* Splits are normalised fractions of the total in [0, 1]. They should sum
  to ~1.0 (we allow a small float-rounding tolerance).
* ``split_type`` is the *user's intent*. The numeric ``share`` values are
  always fractions in [0, 1] regardless of split_type — the parser is
  expected to translate any phrasing into normalised fractions:
    - "Priya 60%, me 40%"   → split_type="percentage", shares 0.6 / 0.4
    - "split 2:1 with Priya" → split_type="shares",     shares 0.667 / 0.333
    - "I had $30, you $17"  → split_type="exact",      shares 30/47, 17/47
    - (no instruction)       → split_type="equal",      shares 1/N each
"""

from __future__ import annotations

from datetime import date as _DateType
from decimal import Decimal
from typing import Literal

from pydantic import BaseModel, Field, field_validator, model_validator

# Tolerance for splits summing to 1.0. Generous enough to accept LLM
# rounding ("0.33, 0.33, 0.34") without rejecting genuine output.
_SPLIT_SUM_TOLERANCE = 0.02


class Split(BaseModel):
    """One participant's share of an expense.

    ``name`` is the name as it appeared in the input (transcript or
    receipt). ``"self"`` is reserved for the payer; otherwise the name
    should match an entry from ``GroupContext.member_names``.
    """

    name: str = Field(min_length=1, max_length=80)
    share: float = Field(ge=0.0, le=1.0)


class LineItem(BaseModel):
    """A single line on the receipt (e.g. "Bananas $2.50").

    Extracted when the parser can read line-by-line off a receipt photo.
    For text-only captures this is usually empty — the user typically
    just says the total, not every item.
    """

    name: str = Field(min_length=1, max_length=120)
    price: Decimal = Field(ge=0)
    quantity: int | None = Field(default=None, ge=1)


class ParsedExpense(BaseModel):
    """Structured output of the receipt-parsing pipeline."""

    amount: Decimal = Field(gt=0)
    currency: str = Field(min_length=3, max_length=3)
    merchant: str | None = Field(default=None, max_length=120)
    # The date printed on the receipt. ``None`` when unreadable (blurry
    # or absent) or when capturing from text-only mode without a date.
    receipt_date: _DateType | None = Field(default=None)
    # Line items from the receipt body. Empty in text-only captures and
    # for receipts where line items are unreadable. Useful as the
    # ``details`` note on the Splitwise expense.
    items: list[LineItem] = Field(default_factory=list)
    # Who actually paid this expense. ``None`` (or ``"self"``) means the
    # sender of the message — that's the common case. A specific name
    # means the user said someone else paid ("Hardik paid for dinner");
    # the orchestrator will route the expense to that user's Splitwise
    # account, and only THEY can confirm it.
    payer: str | None = Field(default=None, max_length=80)
    split_type: Literal["equal", "percentage", "shares", "exact"]
    splits: list[Split] = Field(min_length=1)
    confidence: float = Field(ge=0.0, le=1.0)

    @field_validator("currency")
    @classmethod
    def _normalise_currency(cls, value: str) -> str:
        return value.upper()

    @model_validator(mode="after")
    def _splits_sum_to_one(self) -> ParsedExpense:
        total = sum(split.share for split in self.splits)
        if abs(total - 1.0) > _SPLIT_SUM_TOLERANCE:
            raise ValueError(
                f"splits must sum to ~1.0 (got {total:.4f}, "
                f"tolerance ±{_SPLIT_SUM_TOLERANCE})"
            )
        return self
