"""Thin async wrapper around the (sync) ``splitwise`` SDK.

One ``SplitwiseClient`` instance is constructed per Splitwise user, with
the user's already-decrypted OAuth2 access token. Callers should hold
instances for the duration of a single request — they're cheap to build.

All SDK calls happen inside ``asyncio.to_thread`` so they don't block the
FastAPI event loop. The SDK itself is synchronous and does blocking HTTP
via ``requests-oauthlib``.
"""

from __future__ import annotations

import asyncio
import logging
from decimal import Decimal

from splitwise import Splitwise
from splitwise.expense import Expense
from splitwise.user import ExpenseUser

from app.config import settings
from app.splitwise.models import Split, SplitwiseGroup, SplitwiseUser

log = logging.getLogger(__name__)


class SplitwiseAPIError(Exception):
    """Raised when the Splitwise SDK returns an error object."""


def _build_sdk(token: str) -> Splitwise:
    """Construct a configured ``Splitwise`` SDK instance.

    Splitwise's ``setOAuth2AccessToken`` expects a dict of
    ``{"access_token": ..., "token_type": "bearer"}``, which is exactly
    the shape the token endpoint returns.
    """
    client_id = settings.splitwise_client_id.get_secret_value()
    client_secret = settings.splitwise_client_secret.get_secret_value()
    if not client_id or not client_secret:
        raise RuntimeError("SPLITWISE_CLIENT_ID / SPLITWISE_CLIENT_SECRET unset")
    sdk = Splitwise(client_id, client_secret)
    sdk.setOAuth2AccessToken({"access_token": token, "token_type": "bearer"})
    return sdk


class SplitwiseClient:
    """Per-user async wrapper around the Splitwise SDK."""

    def __init__(self, token: str) -> None:
        if not token:
            raise ValueError("SplitwiseClient requires a non-empty access token")
        # Build the SDK once per client; reuse for the lifetime of this instance.
        self._sdk = _build_sdk(token)

    async def get_current_user(self) -> SplitwiseUser:
        """Return the Splitwise profile of the user this client is authed as."""
        user = await asyncio.to_thread(self._sdk.getCurrentUser)
        if user is None:
            raise SplitwiseAPIError("getCurrentUser returned None")
        return SplitwiseUser(
            id=user.getId(),
            first_name=user.getFirstName(),
            last_name=user.getLastName(),
            email=user.getEmail(),
        )

    async def get_friends(self) -> list[SplitwiseUser]:
        """Return the user's Splitwise friend list as flat SplitwiseUser models."""
        friends = await asyncio.to_thread(self._sdk.getFriends)
        if friends is None:
            return []
        return [
            SplitwiseUser(
                id=f.getId(),
                first_name=f.getFirstName(),
                last_name=f.getLastName(),
                email=f.getEmail(),
            )
            for f in friends
        ]

    async def get_groups(self) -> list[SplitwiseGroup]:
        """Return the user's Splitwise groups (id, name, member ids)."""
        groups = await asyncio.to_thread(self._sdk.getGroups)
        if groups is None:
            return []
        out: list[SplitwiseGroup] = []
        for g in groups:
            members = g.getMembers() or []
            out.append(
                SplitwiseGroup(
                    id=g.getId(),
                    name=g.getName() or "",
                    member_ids=[m.getId() for m in members],
                )
            )
        return out

    async def create_expense(
        self,
        *,
        cost: Decimal,
        currency: str,
        description: str,
        group_id: int | None,
        splits: list[Split],
    ) -> int:
        """Create an expense on Splitwise. Returns the created expense id.

        ``splits`` must already balance: ``sum(paid_share) == sum(owed_share)
        == cost``. We don't enforce that here — Splitwise will reject mismatches
        and surface the error via the returned error object.
        """
        if not splits:
            raise ValueError("create_expense requires at least one split")

        expense = Expense()
        expense.setCost(str(cost))
        expense.setCurrencyCode(currency)
        expense.setDescription(description)
        if group_id is not None:
            expense.setGroupId(group_id)

        for s in splits:
            eu = ExpenseUser()
            eu.setId(s.splitwise_user_id)
            eu.setPaidShare(str(s.paid_share))
            eu.setOwedShare(str(s.owed_share))
            expense.addUser(eu)

        # ``createExpense`` returns ``(expense, errors)`` — both may be None.
        created, errors = await asyncio.to_thread(self._sdk.createExpense, expense)
        if errors is not None:
            # ``errors`` may be a ``SplitwiseError`` with ``getErrors()`` or a
            # bare dict — coerce to something log-safe without leaking the token.
            detail = errors.getErrors() if hasattr(errors, "getErrors") else str(errors)
            log.warning("Splitwise create_expense returned errors: %s", detail)
            raise SplitwiseAPIError(f"create_expense failed: {detail}")
        if created is None:
            raise SplitwiseAPIError("create_expense returned no expense object")
        return int(created.getId())


__all__ = ["SplitwiseAPIError", "SplitwiseClient"]
