"""Pydantic models for the slice of the Splitwise API we care about.

These are deliberately narrow — we only model the fields we *use*. The
underlying ``splitwise`` SDK objects expose far more; the rest of the app
should never touch those raw objects so we can swap SDKs without ripple.
"""

from __future__ import annotations

from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field


class _SwModel(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class SplitwiseUser(_SwModel):
    """A Splitwise user (current user or a friend)."""

    id: int
    first_name: str | None = None
    last_name: str | None = None
    email: str | None = None


class SplitwiseGroup(_SwModel):
    """A Splitwise group the user belongs to."""

    id: int
    name: str
    member_ids: list[int] = Field(default_factory=list)


class Split(_SwModel):
    """One user's share of an expense.

    ``paid_share`` and ``owed_share`` are decimal currency amounts (not
    percentages). For an equal 2-way $20 split where Alice paid: Alice
    has ``paid_share=20, owed_share=10``; Bob has ``paid_share=0,
    owed_share=10``.

    Splitwise requires the sums to balance: sum(paid_share) == sum(owed_share)
    == total cost. The orchestrator (Brick F) builds these.
    """

    splitwise_user_id: int
    paid_share: Decimal = Decimal("0")
    owed_share: Decimal = Decimal("0")


__all__ = ["Split", "SplitwiseGroup", "SplitwiseUser"]
