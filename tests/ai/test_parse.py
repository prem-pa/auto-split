"""Tests for the Gemini receipt parser.

We never hit the real Gemini API. A ``FakeGenaiClient`` records the
call arguments and returns canned text responses.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

import pytest

from app.ai.factory import default_parser
from app.ai.parse import GeminiExpenseParser, _detect_image_mime
from app.ai.provider import GroupContext, ParserError
from app.ai.schema import ParsedExpense
from tests.ai.conftest import FAKE_JPEG_BYTES, FAKE_PNG_BYTES


@dataclass
class _FakeResponse:
    text: str


@dataclass
class _RecordedCall:
    model: str
    contents: list[Any]
    config: Any


class _FakeAsyncModels:
    def __init__(self, responses: list[str | Exception]) -> None:
        self._responses = list(responses)
        self.calls: list[_RecordedCall] = []

    async def generate_content(
        self, *, model: str, contents: list[Any], config: Any
    ) -> _FakeResponse:
        self.calls.append(_RecordedCall(model=model, contents=contents, config=config))
        if not self._responses:
            raise AssertionError("FakeGenaiClient ran out of canned responses")
        nxt = self._responses.pop(0)
        if isinstance(nxt, Exception):
            raise nxt
        return _FakeResponse(text=nxt)


@dataclass
class _FakeAio:
    models: _FakeAsyncModels = field(default_factory=lambda: _FakeAsyncModels([]))


@dataclass
class _FakeGenaiClient:
    aio: _FakeAio = field(default_factory=_FakeAio)

    @classmethod
    def with_responses(cls, *responses: str | Exception) -> "_FakeGenaiClient":
        return cls(aio=_FakeAio(models=_FakeAsyncModels(list(responses))))


# ---------------------------------------------------------------------------
# image mime detection
# ---------------------------------------------------------------------------


def test_detect_image_mime_jpeg() -> None:
    assert _detect_image_mime(FAKE_JPEG_BYTES) == "image/jpeg"


def test_detect_image_mime_png() -> None:
    assert _detect_image_mime(FAKE_PNG_BYTES) == "image/png"


def test_detect_image_mime_fallback() -> None:
    assert _detect_image_mime(b"nothing recognisable here") == "image/jpeg"


# ---------------------------------------------------------------------------
# happy path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_returns_parsed_expense_on_valid_json(
    canned_expense_json: str, group_context: GroupContext
) -> None:
    client = _FakeGenaiClient.with_responses(canned_expense_json)
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]

    result = await parser.parse(FAKE_JPEG_BYTES, "Split with Priya", group_context)
    assert isinstance(result, ParsedExpense)
    assert result.amount > 0
    assert len(result.splits) == 2
    assert sum(s.share for s in result.splits) == pytest.approx(1.0)
    assert len(client.aio.models.calls) == 1


@pytest.mark.asyncio
async def test_parse_sends_structured_output_schema(
    canned_expense_json: str, group_context: GroupContext
) -> None:
    client = _FakeGenaiClient.with_responses(canned_expense_json)
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]

    await parser.parse(FAKE_JPEG_BYTES, None, group_context)

    call = client.aio.models.calls[0]
    config = call.config
    assert config.response_mime_type == "application/json"
    assert config.response_schema == ParsedExpense.model_json_schema()
    # System prompt is set on the config, not in contents.
    assert config.system_instruction is not None
    assert "JSON" in config.system_instruction


@pytest.mark.asyncio
async def test_parse_includes_payer_members_and_currency_in_user_prompt(
    canned_expense_json: str,
) -> None:
    ctx = GroupContext(
        payer_name="Prem",
        member_names=["Priya", "Cody"],
        default_currency="EUR",
    )
    client = _FakeGenaiClient.with_responses(canned_expense_json)
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]

    await parser.parse(FAKE_JPEG_BYTES, "split with priya", ctx)

    contents = client.aio.models.calls[0].contents
    user_text = next(c for c in contents if isinstance(c, str))
    assert "Prem" in user_text
    assert "Priya" in user_text
    assert "Cody" in user_text
    assert "EUR" in user_text
    assert "split with priya" in user_text


@pytest.mark.asyncio
async def test_parse_omits_transcript_when_none(
    canned_expense_json: str, group_context: GroupContext
) -> None:
    client = _FakeGenaiClient.with_responses(canned_expense_json)
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]

    await parser.parse(FAKE_JPEG_BYTES, None, group_context)
    user_text = next(
        c for c in client.aio.models.calls[0].contents if isinstance(c, str)
    )
    assert "no voice note" in user_text.lower() or "no text" in user_text.lower()


# ---------------------------------------------------------------------------
# retry + error path
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_parse_retries_once_on_invalid_json(
    canned_expense_json: str, group_context: GroupContext
) -> None:
    client = _FakeGenaiClient.with_responses("not json at all", canned_expense_json)
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]

    result = await parser.parse(FAKE_JPEG_BYTES, "x", group_context)
    assert isinstance(result, ParsedExpense)
    assert len(client.aio.models.calls) == 2

    # Second call should carry the stricter retry instruction.
    second = client.aio.models.calls[1]
    assert "VALID JSON" in second.config.system_instruction


@pytest.mark.asyncio
async def test_parse_raises_after_failed_retry(group_context: GroupContext) -> None:
    client = _FakeGenaiClient.with_responses("garbage one", "garbage two")
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]

    with pytest.raises(ParserError):
        await parser.parse(FAKE_JPEG_BYTES, "x", group_context)
    assert len(client.aio.models.calls) == 2


@pytest.mark.asyncio
async def test_parse_rejects_when_splits_do_not_sum_to_one(
    group_context: GroupContext,
) -> None:
    bad = json.dumps(
        {
            "amount": "10.00",
            "currency": "USD",
            "merchant": "x",
            "split_type": "equal",
            "splits": [
                {"name": "self", "share": 0.2},
                {"name": "Priya", "share": 0.2},
            ],
            "confidence": 0.9,
        }
    )
    # Same bad JSON twice — retry can't save us.
    client = _FakeGenaiClient.with_responses(bad, bad)
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]
    with pytest.raises(ParserError):
        await parser.parse(FAKE_JPEG_BYTES, "x", group_context)


@pytest.mark.asyncio
async def test_parse_rejects_empty_image(group_context: GroupContext) -> None:
    client = _FakeGenaiClient.with_responses("{}")
    parser = GeminiExpenseParser(client=client)  # type: ignore[arg-type]
    with pytest.raises(ParserError):
        await parser.parse(b"", None, group_context)


# ---------------------------------------------------------------------------
# factory
# ---------------------------------------------------------------------------


def test_default_parser_returns_a_gemini_parser() -> None:
    # The settings fixture provides a fake key so this should succeed.
    parser = default_parser()
    assert isinstance(parser, GeminiExpenseParser)
