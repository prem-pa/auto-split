"""Tests for the ``ExpenseParser`` Protocol and ``GroupContext`` dataclass."""

from __future__ import annotations

from decimal import Decimal

import pytest

from app.ai.provider import ExpenseParser, GroupContext, ParserError
from app.ai.schema import ParsedExpense, Split


class FakeParser:
    """Hand-rolled parser used to prove the Protocol is duck-typeable."""

    def __init__(self, result: ParsedExpense) -> None:
        self._result = result
        self.calls: list[tuple[bytes, str | None, GroupContext]] = []

    async def parse(
        self,
        image_bytes: bytes,
        transcript: str | None,
        context: GroupContext,
    ) -> ParsedExpense:
        self.calls.append((image_bytes, transcript, context))
        return self._result


@pytest.mark.asyncio
async def test_fake_parser_satisfies_protocol(group_context: GroupContext) -> None:
    canned = ParsedExpense(
        amount=Decimal("10.00"),
        currency="USD",
        merchant="Test",
        split_type="equal",
        splits=[Split(name="self", share=1.0)],
        confidence=0.99,
    )
    parser: ExpenseParser = FakeParser(canned)
    assert isinstance(parser, ExpenseParser)

    result = await parser.parse(b"fake-bytes", "hello", group_context)
    assert result is canned
    assert len(parser.calls) == 1  # type: ignore[attr-defined]


def test_group_context_is_immutable() -> None:
    ctx = GroupContext("me", ["a"], "USD")
    with pytest.raises(Exception):  # frozen dataclass -> FrozenInstanceError
        ctx.payer_name = "other"  # type: ignore[misc]


def test_parser_error_is_runtime_error() -> None:
    assert issubclass(ParserError, RuntimeError)
