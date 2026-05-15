"""Tests for the prompt builders."""

from __future__ import annotations

from app.ai.prompts import (
    STRICT_RETRY_SUFFIX,
    SYSTEM_PROMPT,
    build_user_prompt,
)
from app.ai.provider import GroupContext


def test_user_prompt_contains_payer_members_currency_and_transcript() -> None:
    ctx = GroupContext(
        payer_name="Prem",
        member_names=["Priya", "Cody"],
        default_currency="EUR",
    )
    prompt = build_user_prompt("Split this with Priya", ctx)

    assert "Prem" in prompt
    assert "Priya" in prompt
    assert "Cody" in prompt
    assert "EUR" in prompt
    assert "Split this with Priya" in prompt


def test_user_prompt_handles_missing_transcript() -> None:
    ctx = GroupContext(
        payer_name="Prem", member_names=["Priya"], default_currency="USD"
    )
    prompt = build_user_prompt(None, ctx)
    assert "no voice note" in prompt.lower() or "no text" in prompt.lower()


def test_user_prompt_handles_empty_member_list() -> None:
    ctx = GroupContext(payer_name="Prem", member_names=[], default_currency="USD")
    prompt = build_user_prompt("dinner", ctx)
    # Should not crash, and should still mention the payer.
    assert "Prem" in prompt


def test_system_prompt_mentions_self_and_schema_rules() -> None:
    # Light contract on the system prompt — these are the load-bearing
    # instructions the rest of the system relies on.
    assert "self" in SYSTEM_PROMPT
    assert "JSON" in SYSTEM_PROMPT
    assert "sum to 1.0" in SYSTEM_PROMPT or "sum to 1" in SYSTEM_PROMPT


def test_strict_retry_suffix_demands_json_only() -> None:
    assert "JSON" in STRICT_RETRY_SUFFIX
