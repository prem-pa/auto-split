"""Tests for the ``ParsedExpense`` / ``Split`` Pydantic models."""

from __future__ import annotations

from decimal import Decimal

import pytest
from pydantic import ValidationError

from app.ai.schema import ParsedExpense, Split


def test_round_trip_from_canned_json(canned_expense_json: str) -> None:
    parsed = ParsedExpense.model_validate_json(canned_expense_json)
    assert parsed.amount == Decimal("47.32")
    assert parsed.currency == "USD"
    assert parsed.merchant == "Trader Joe's"
    assert parsed.split_type == "equal"
    assert len(parsed.splits) == 2
    assert sum(s.share for s in parsed.splits) == pytest.approx(1.0)

    # Round-trip through JSON and re-validate — proves serialisation is
    # symmetric with deserialisation.
    rehydrated = ParsedExpense.model_validate_json(parsed.model_dump_json())
    assert rehydrated == parsed


def test_currency_is_uppercased() -> None:
    parsed = ParsedExpense.model_validate(
        {
            "amount": "10.00",
            "currency": "usd",
            "merchant": None,
            "split_type": "equal",
            "splits": [{"name": "self", "share": 1.0}],
            "confidence": 0.9,
        }
    )
    assert parsed.currency == "USD"


def test_rejects_malformed_json() -> None:
    with pytest.raises(ValidationError):
        ParsedExpense.model_validate_json("{not json")


def test_rejects_splits_not_summing_to_one() -> None:
    with pytest.raises(ValidationError) as exc:
        ParsedExpense.model_validate(
            {
                "amount": "10.00",
                "currency": "USD",
                "merchant": "x",
                "split_type": "equal",
                "splits": [
                    {"name": "self", "share": 0.3},
                    {"name": "Priya", "share": 0.3},
                ],
                "confidence": 0.9,
            }
        )
    assert "splits must sum" in str(exc.value)


def test_accepts_small_rounding_error() -> None:
    # Three-way equal split with two-decimal rounding: 0.33 + 0.33 + 0.34
    parsed = ParsedExpense.model_validate(
        {
            "amount": "30.00",
            "currency": "USD",
            "merchant": "x",
            "split_type": "equal",
            "splits": [
                {"name": "self", "share": 0.33},
                {"name": "A", "share": 0.33},
                {"name": "B", "share": 0.34},
            ],
            "confidence": 0.9,
        }
    )
    assert len(parsed.splits) == 3


def test_rejects_negative_amount() -> None:
    with pytest.raises(ValidationError):
        ParsedExpense.model_validate(
            {
                "amount": "-1.00",
                "currency": "USD",
                "merchant": None,
                "split_type": "equal",
                "splits": [{"name": "self", "share": 1.0}],
                "confidence": 0.9,
            }
        )


def test_accepts_shares_split_type() -> None:
    """Ratio-based splits (e.g. "2:1") are represented with split_type='shares'."""
    parsed = ParsedExpense.model_validate(
        {
            "amount": "30.00",
            "currency": "USD",
            "merchant": "Diner",
            "split_type": "shares",
            "splits": [
                {"name": "self", "share": 0.6667},
                {"name": "Priya", "share": 0.3333},
            ],
            "confidence": 0.9,
        }
    )
    assert parsed.split_type == "shares"
    assert sum(s.share for s in parsed.splits) == pytest.approx(1.0, abs=0.01)


def test_rejects_share_out_of_range() -> None:
    with pytest.raises(ValidationError):
        Split(name="self", share=1.5)
    with pytest.raises(ValidationError):
        Split(name="self", share=-0.1)


def test_model_json_schema_is_well_formed() -> None:
    """Used as the Gemini structured-output schema — must produce one."""
    schema = ParsedExpense.model_json_schema()
    assert schema["type"] == "object"
    assert "amount" in schema["properties"]
    assert "splits" in schema["properties"]
