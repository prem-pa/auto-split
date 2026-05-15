"""Shared fixtures for Brick F (orchestrator) tests.

Strategy:
    * Never touch the real Telegram, Splitwise, Groq, Gemini, or Supabase APIs.
    * Patch every boundary that the pipeline crosses into other bricks:
        - ``app.telegram.send_message`` / ``edit_message`` / ``download_telegram_file``
        - ``app.ai.default_parser`` / ``app.ai.transcribe_voice``
        - ``app.splitwise.SplitwiseClient`` / ``app.splitwise.load_user_token``
        - ``app.db.*`` CRUD functions
    * Use real Pydantic models (``ParsedExpense``, ``User``…) — they're cheap.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import SecretStr

from app.ai.provider import GroupContext
from app.ai.schema import ParsedExpense, Split
from app.db.models import ExpensePending, User


# ---------------------------------------------------------------------------
# Settings — pin everything Brick F transitively reads.
# ---------------------------------------------------------------------------

# Real Fernet key so mint_state / verify_state work end-to-end.
from cryptography.fernet import Fernet  # noqa: E402

TEST_FERNET_KEY = Fernet.generate_key().decode()


@pytest.fixture(autouse=True)
def _patch_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "telegram_bot_token", SecretStr("123:TEST"))
    monkeypatch.setattr(settings, "telegram_webhook_secret", SecretStr("test-secret"))
    monkeypatch.setattr(settings, "public_base_url", "https://bot.example.test")
    monkeypatch.setattr(settings, "splitwise_client_id", SecretStr("cid"))
    monkeypatch.setattr(settings, "splitwise_client_secret", SecretStr("csec"))
    monkeypatch.setattr(settings, "token_encryption_key", SecretStr(TEST_FERNET_KEY))
    monkeypatch.setattr(settings, "groq_api_key", SecretStr("groq"))
    monkeypatch.setattr(settings, "gemini_api_key", SecretStr("gem"))
    monkeypatch.setattr(settings, "telegram_allowed_user_ids", [])


@pytest.fixture(autouse=True)
def _reset_fernet() -> Iterator[None]:
    from app.splitwise.tokens import _fernet

    _fernet.cache_clear()
    yield
    _fernet.cache_clear()


# ---------------------------------------------------------------------------
# Captures
# ---------------------------------------------------------------------------


@dataclass
class SentMessage:
    chat_id: int
    text: str
    reply_markup: Any | None


@dataclass
class EditedMessage:
    chat_id: int
    message_id: int
    text: str
    reply_markup: Any | None


@dataclass
class CreatedPending:
    telegram_message_id: int | None
    telegram_group_id: int | None
    payer_telegram_user_id: int | None
    parsed_data: dict[str, Any]


@dataclass
class CreatedExpense:
    cost: Decimal
    currency: str
    description: str
    group_id: int | None
    splits: list[Any]


@dataclass
class PipelineMocks:
    """One-stop bag of doubles for orchestrator tests."""

    sent: list[SentMessage] = field(default_factory=list)
    edits: list[EditedMessage] = field(default_factory=list)
    pendings: dict[UUID, ExpensePending] = field(default_factory=dict)
    created_pendings: list[CreatedPending] = field(default_factory=list)
    deleted_pendings: list[UUID] = field(default_factory=list)
    marked_completed: list[tuple[UUID, int]] = field(default_factory=list)
    memberships: list[tuple[int, int]] = field(default_factory=list)
    parser_calls: list[tuple[bytes, str | None, GroupContext]] = field(
        default_factory=list
    )
    transcribed: list[tuple[bytes, str]] = field(default_factory=list)
    downloads: list[str] = field(default_factory=list)
    sw_create_calls: list[CreatedExpense] = field(default_factory=list)
    sw_clients_constructed: list[str] = field(default_factory=list)
    users: dict[int, User] = field(default_factory=dict)
    group_members: dict[int, list[User]] = field(default_factory=dict)
    tokens: dict[int, tuple[str, int]] = field(default_factory=dict)
    sweep_calls: list[None] = field(default_factory=list)
    sweep_return: int = 0

    parsed_expense: ParsedExpense | None = None
    transcript_return: str = ""
    sw_create_return: int = 99001
    parser_should_raise: Exception | None = None


@pytest.fixture
def mocks(monkeypatch: pytest.MonkeyPatch) -> PipelineMocks:
    """Patch every boundary and return the capture buckets."""
    m = PipelineMocks()

    # Default parsed expense — tests overwrite when needed.
    m.parsed_expense = ParsedExpense(
        amount=Decimal("47.32"),
        currency="USD",
        merchant="Trader Joe's",
        split_type="equal",
        splits=[
            Split(name="self", share=0.5),
            Split(name="Priya", share=0.5),
        ],
        confidence=0.9,
    )

    # --- Telegram ---------------------------------------------------------

    async def fake_send_message(
        chat_id: int, text: str, reply_markup: Any | None = None
    ) -> dict[str, Any]:
        m.sent.append(
            SentMessage(chat_id=chat_id, text=text, reply_markup=reply_markup)
        )

        # Return a fake ``Message``-shaped object so callers that read
        # ``.message_id`` from our reply don't crash. We only use the
        # dict form internally; production code paths don't depend on it.
        return {"message_id": 9999}

    async def fake_edit_message(
        chat_id: int, message_id: int, text: str, reply_markup: Any | None = None
    ) -> dict[str, Any]:
        m.edits.append(
            EditedMessage(
                chat_id=chat_id,
                message_id=message_id,
                text=text,
                reply_markup=reply_markup,
            )
        )
        return {"message_id": message_id}

    async def fake_download(file_id: str) -> bytes:
        m.downloads.append(file_id)
        # JPEG-like prefix so Brick D's magic-byte sniff is happy if it
        # ever runs against our stub.
        if file_id.startswith("voice"):
            return b"OggS\x00\x02" + b"\x00" * 32
        return b"\xff\xd8\xff" + b"\x00" * 64

    # The pipeline accesses these via ``_tg_bot.send_message`` /
    # ``_tg_files.download_telegram_file`` (module-attribute lookup at call
    # time), so patching the source module is enough.
    monkeypatch.setattr("app.telegram.bot.send_message", fake_send_message)
    monkeypatch.setattr("app.telegram.bot.edit_message", fake_edit_message)
    monkeypatch.setattr("app.telegram.files.download_telegram_file", fake_download)

    # --- AI parsing / transcription --------------------------------------

    class _FakeParser:
        async def parse(
            self,
            image_bytes: bytes,
            transcript: str | None,
            context: GroupContext,
        ) -> ParsedExpense:
            m.parser_calls.append((image_bytes, transcript, context))
            if m.parser_should_raise is not None:
                raise m.parser_should_raise
            assert m.parsed_expense is not None
            return m.parsed_expense

    def fake_default_parser() -> _FakeParser:
        return _FakeParser()

    async def fake_transcribe(audio: bytes, mime: str) -> str:
        m.transcribed.append((audio, mime))
        return m.transcript_return

    monkeypatch.setattr(
        "app.services.expense_pipeline.default_parser", fake_default_parser
    )
    monkeypatch.setattr(
        "app.services.expense_pipeline.transcribe_voice", fake_transcribe
    )

    # --- Splitwise --------------------------------------------------------

    async def fake_load_user_token(
        telegram_user_id: int,
    ) -> tuple[str, int] | None:
        return m.tokens.get(telegram_user_id)

    class _FakeSwClient:
        def __init__(self, token: str) -> None:
            m.sw_clients_constructed.append(token)

        async def create_expense(
            self,
            *,
            cost: Decimal,
            currency: str,
            description: str,
            group_id: int | None,
            splits: list[Any],
        ) -> int:
            m.sw_create_calls.append(
                CreatedExpense(
                    cost=cost,
                    currency=currency,
                    description=description,
                    group_id=group_id,
                    splits=splits,
                )
            )
            return m.sw_create_return

    monkeypatch.setattr(
        "app.services.expense_pipeline.load_user_token", fake_load_user_token
    )
    monkeypatch.setattr("app.services.expense_pipeline.SplitwiseClient", _FakeSwClient)

    # --- DB CRUD ----------------------------------------------------------

    async def fake_record_membership(
        telegram_user_id: int, telegram_group_id: int
    ) -> None:
        m.memberships.append((telegram_user_id, telegram_group_id))

    async def fake_list_group_members(telegram_group_id: int) -> list[User]:
        return m.group_members.get(telegram_group_id, [])

    async def fake_get_user(telegram_user_id: int) -> User | None:
        return m.users.get(telegram_user_id)

    async def fake_create_pending(
        *,
        telegram_message_id: int | None,
        telegram_group_id: int | None,
        payer_telegram_user_id: int | None,
        parsed_data: dict[str, Any],
        receipt_image_url: str | None = None,
        expires_at: Any | None = None,
    ) -> ExpensePending:
        m.created_pendings.append(
            CreatedPending(
                telegram_message_id=telegram_message_id,
                telegram_group_id=telegram_group_id,
                payer_telegram_user_id=payer_telegram_user_id,
                parsed_data=parsed_data,
            )
        )
        pid = uuid4()
        pending = ExpensePending(
            id=pid,
            telegram_message_id=telegram_message_id,
            telegram_group_id=telegram_group_id,
            payer_telegram_user_id=payer_telegram_user_id,
            parsed_data=parsed_data,
        )
        m.pendings[pid] = pending
        return pending

    async def fake_get_pending(id: UUID) -> ExpensePending | None:
        return m.pendings.get(id)

    async def fake_delete_pending(id: UUID) -> None:
        m.deleted_pendings.append(id)
        m.pendings.pop(id, None)

    async def fake_mark_completed(
        pending_id: UUID,
        splitwise_expense_id: int,
        *,
        amount_cents: int | None = None,
        currency: str | None = None,
    ) -> Any:
        m.marked_completed.append((pending_id, splitwise_expense_id))
        m.pendings.pop(pending_id, None)
        return None

    monkeypatch.setattr(
        "app.services.expense_pipeline.record_membership", fake_record_membership
    )
    monkeypatch.setattr(
        "app.services.expense_pipeline.list_group_members", fake_list_group_members
    )
    monkeypatch.setattr(
        "app.services.expense_pipeline.create_pending", fake_create_pending
    )
    monkeypatch.setattr("app.services.expense_pipeline.get_pending", fake_get_pending)
    monkeypatch.setattr(
        "app.services.expense_pipeline.delete_pending", fake_delete_pending
    )
    monkeypatch.setattr(
        "app.services.expense_pipeline.mark_completed", fake_mark_completed
    )
    # group_context goes through Brick E directly.
    monkeypatch.setattr(
        "app.services.group_context.list_group_members", fake_list_group_members
    )
    monkeypatch.setattr("app.services.group_context.get_user", fake_get_user)

    return m
