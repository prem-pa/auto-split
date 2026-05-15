"""Pydantic models for the 5 domain tables.

These models are the typed shape we hand back to callers. They round-trip
cleanly with the dict-shape rows that ``supabase-py`` returns (timestamps
arrive as ISO-8601 strings, UUIDs as strings; Pydantic v2 parses both).

Naming convention: fields mirror the SQL columns 1:1.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field


class _DBModel(BaseModel):
    """Shared base — permissive parsing for Supabase row shapes."""

    model_config = ConfigDict(
        # Supabase rows may contain columns we don't model yet; ignore them
        # rather than failing.
        extra="ignore",
        # Allow constructing from ORM-style objects too, just in case.
        from_attributes=True,
    )


class User(_DBModel):
    telegram_user_id: int
    telegram_username: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    splitwise_user_id: int | None = None
    splitwise_access_token: str | None = None
    default_currency: str = "USD"
    created_at: datetime | None = None


class Group(_DBModel):
    telegram_group_id: int
    telegram_group_name: str | None = None
    splitwise_group_id: int | None = None
    created_at: datetime | None = None


class GroupMembership(_DBModel):
    telegram_user_id: int
    telegram_group_id: int
    last_seen_at: datetime | None = None


class ExpensePending(_DBModel):
    id: UUID
    telegram_message_id: int | None = None
    telegram_group_id: int | None = None
    payer_telegram_user_id: int | None = None
    parsed_data: dict[str, Any] | None = Field(default=None)
    receipt_image_url: str | None = None
    expires_at: datetime | None = None


class ExpenseCompleted(_DBModel):
    id: UUID
    splitwise_expense_id: int | None = None
    telegram_message_id: int | None = None
    telegram_group_id: int | None = None
    payer_telegram_user_id: int | None = None
    amount_cents: int | None = None
    currency: str | None = None
    created_at: datetime | None = None


__all__ = [
    "ExpenseCompleted",
    "ExpensePending",
    "Group",
    "GroupMembership",
    "User",
]
