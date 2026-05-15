"""Inline-keyboard builders + callback_data format."""

from __future__ import annotations

import re
from uuid import UUID, uuid4

import pytest

from app.telegram import keyboards

# 2-char prefix + ":" + 32-char hex
_PATTERN = re.compile(r"^(cf|ed|cx):[0-9a-f]{32}$")
_TELEGRAM_CALLBACK_DATA_LIMIT = 64


@pytest.mark.parametrize(
    ("builder", "prefix"),
    [
        (keyboards.confirm_keyboard, "cf"),
        (keyboards.edit_keyboard, "ed"),
        (keyboards.cancel_keyboard, "cx"),
    ],
)
def test_single_button_keyboards_callback_data_format(
    builder: object, prefix: str
) -> None:
    pending_id = uuid4()
    markup = builder(pending_id)  # type: ignore[operator]
    buttons = markup.inline_keyboard
    assert len(buttons) == 1, "single row"
    assert len(buttons[0]) == 1, "single button"
    data = buttons[0][0].callback_data
    assert isinstance(data, str)
    assert data == f"{prefix}:{pending_id.hex}"
    assert _PATTERN.match(data) is not None
    assert len(data.encode("utf-8")) <= _TELEGRAM_CALLBACK_DATA_LIMIT
    # 2-char prefix + ":" + 32-char hex = 35 chars
    assert len(data) == 35


def test_combined_keyboard_has_all_three_actions() -> None:
    pending_id = uuid4()
    markup = keyboards.confirmation_keyboard(pending_id)
    row = markup.inline_keyboard[0]
    assert len(row) == 3
    prefixes = [btn.callback_data.split(":", 1)[0] for btn in row]
    assert prefixes == ["cf", "ed", "cx"]
    # All three carry the same pending_id
    suffixes = {btn.callback_data.split(":", 1)[1] for btn in row}
    assert suffixes == {pending_id.hex}


def test_callback_data_is_ascii_and_under_limit_for_max_uuid() -> None:
    # UUIDs are fixed 32 hex chars regardless of value; this proves the
    # length invariant holds for the upper-bound id too.
    upper = UUID(int=(1 << 128) - 1)
    for builder in (
        keyboards.confirm_keyboard,
        keyboards.edit_keyboard,
        keyboards.cancel_keyboard,
    ):
        data = builder(upper).inline_keyboard[0][0].callback_data
        assert data.isascii()
        assert len(data.encode("utf-8")) <= _TELEGRAM_CALLBACK_DATA_LIMIT
