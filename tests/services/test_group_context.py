"""Tests for :mod:`app.services.group_context`."""

from __future__ import annotations

import pytest

from app.ai.schema import Split
from app.db.models import User
from app.services import group_context
from app.services.group_context import (
    UNRESOLVED,
    ResolvedSplit,
    build_group_context,
    resolve_split_names,
)
from tests.services.conftest import PipelineMocks


@pytest.mark.asyncio
async def test_build_group_context_dm_has_no_other_members(
    mocks: PipelineMocks,
) -> None:
    mocks.users[7] = User(
        telegram_user_id=7, telegram_username="prem", default_currency="EUR"
    )

    ctx = await build_group_context(None, 7)
    assert ctx.payer_name == "prem"
    assert ctx.member_names == []
    assert ctx.default_currency == "EUR"


@pytest.mark.asyncio
async def test_build_group_context_group_lists_other_members(
    mocks: PipelineMocks,
) -> None:
    mocks.users[7] = User(telegram_user_id=7, telegram_username="prem")
    mocks.group_members[-100] = [
        mocks.users[7],
        User(telegram_user_id=8, telegram_username="priya"),
        User(telegram_user_id=9, telegram_username="cody"),
    ]

    ctx = await build_group_context(-100, 7)

    assert ctx.payer_name == "prem"
    # Payer excluded; other two listed.
    assert sorted(ctx.member_names) == ["cody", "priya"]


@pytest.mark.asyncio
async def test_build_group_context_handles_missing_payer(
    mocks: PipelineMocks,
) -> None:
    """Unknown payer (no user row) still produces a sane context."""
    ctx = await build_group_context(None, 555)
    assert ctx.payer_name == "self"
    assert ctx.default_currency == "USD"


# ---------------------------------------------------------------------------
# resolve_split_names
# ---------------------------------------------------------------------------


def _u(uid: int, username: str, sw_id: int | None = None) -> User:
    return User(
        telegram_user_id=uid, telegram_username=username, splitwise_user_id=sw_id
    )


def test_resolve_self_token_maps_to_payer() -> None:
    out = resolve_split_names(
        [Split(name="self", share=0.5)],
        [],
        payer_telegram_user_id=42,
        payer_splitwise_user_id=999,
    )
    assert out == [
        ResolvedSplit(
            name="self",
            share=0.5,
            telegram_user_id=42,
            splitwise_user_id=999,
            ambiguous=False,
        )
    ]


def test_resolve_exact_username_match() -> None:
    members = [_u(7, "prem", 1), _u(8, "priya", 2)]
    out = resolve_split_names(
        [Split(name="Priya", share=0.5)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert len(out) == 1
    assert out[0].telegram_user_id == 8
    assert out[0].splitwise_user_id == 2
    assert not out[0].ambiguous


def test_resolve_prefix_match_when_unique() -> None:
    members = [_u(8, "priya_p", 2)]
    out = resolve_split_names(
        [Split(name="priya", share=0.5)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].telegram_user_id == 8


def test_resolve_returns_unresolved_for_unknown_name() -> None:
    out = resolve_split_names(
        [Split(name="bogus", share=0.5)],
        [_u(8, "priya", 2)],
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].telegram_user_id == UNRESOLVED
    assert out[0].splitwise_user_id is None


def test_resolve_marks_ambiguous_prefix_matches() -> None:
    members = [_u(8, "priya_a", 2), _u(9, "priya_b", 3)]
    out = resolve_split_names(
        [Split(name="priya", share=0.5)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].ambiguous
    assert out[0].telegram_user_id == UNRESOLVED


def test_resolve_is_case_and_punctuation_insensitive() -> None:
    members = [_u(8, "Priya-Patel", 2)]
    out = resolve_split_names(
        [Split(name="priyapatel", share=0.5)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].telegram_user_id == 8


def test_module_exports_helpers() -> None:
    # Quick sanity that ``__all__`` matches what we import in callers.
    for sym in ("build_group_context", "resolve_split_names", "UNRESOLVED"):
        assert hasattr(group_context, sym)


# --- first_name / last_name matching --------------------------------------


def test_resolve_matches_by_first_name_when_username_is_none() -> None:
    """The motivating bug: 'Shreya' must match a user whose
    telegram_username is null but first_name='Shreya'.
    """
    members = [
        User(
            telegram_user_id=8,
            telegram_username=None,
            first_name="Shreya",
            splitwise_user_id=2,
        )
    ]
    out = resolve_split_names(
        [Split(name="Shreya", share=0.5), Split(name="self", share=0.5)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].telegram_user_id == 8
    assert out[0].splitwise_user_id == 2
    assert not out[0].ambiguous


def test_resolve_matches_by_first_name_even_when_username_is_unrelated() -> None:
    """'Shreya' should still resolve when username is e.g. 'racecar99'."""
    members = [
        User(
            telegram_user_id=8,
            telegram_username="racecar99",
            first_name="Shreya",
            splitwise_user_id=2,
        )
    ]
    out = resolve_split_names(
        [Split(name="Shreya", share=1.0)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].telegram_user_id == 8


def test_resolve_dedupes_when_single_user_matches_multiple_candidates() -> None:
    """A user with first_name='Shreya' AND username='shreya' should
    still count as ONE match (not flagged as ambiguous)."""
    members = [
        User(
            telegram_user_id=8,
            telegram_username="shreya",
            first_name="Shreya",
            splitwise_user_id=2,
        )
    ]
    out = resolve_split_names(
        [Split(name="shreya", share=1.0)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert not out[0].ambiguous
    assert out[0].telegram_user_id == 8


def test_resolve_username_still_works_as_fallback() -> None:
    """If only telegram_username is set (no first_name), match still works."""
    members = [
        User(
            telegram_user_id=8,
            telegram_username="priya_p",
            first_name=None,
            splitwise_user_id=2,
        )
    ]
    out = resolve_split_names(
        [Split(name="priya", share=1.0)],
        members,
        payer_telegram_user_id=7,
        payer_splitwise_user_id=1,
    )
    assert out[0].telegram_user_id == 8  # prefix-matched "priya_p"
