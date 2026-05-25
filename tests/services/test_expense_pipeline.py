"""Tests for :mod:`app.services.expense_pipeline`.

Every external boundary is mocked; we exercise the orchestrator's
own decisions (routing, error paths, payload shape, idempotency).
"""

from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import uuid4

import pytest
from telegram import Bot, Update

from app.ai.schema import ParsedExpense, Split
from app.db.models import User
from app.services import expense_pipeline
from app.telegram import keyboards
from tests.services.conftest import PipelineMocks


# ---------------------------------------------------------------------------
# Helpers for building fake Update payloads.
# ---------------------------------------------------------------------------


def _fake_bot() -> Bot:
    # Bot needs a token to construct, but we never call its network methods.
    return Bot(token="123:TEST")


def _text_update(
    text: str,
    *,
    user_id: int = 7,
    chat_id: int | None = None,
    chat_type: str = "private",
) -> Update:
    """Build a Telegram text Update.

    Defaults to a DM (chat_id == user_id). Pass ``chat_id`` (typically a
    negative number for Telegram groups) + ``chat_type="group"`` to
    simulate an @-mention in a group.
    """
    cid = chat_id if chat_id is not None else user_id
    return Update.de_json(
        {
            "update_id": 1,
            "message": {
                "message_id": 10,
                "date": 1_700_000_000,
                "chat": {"id": cid, "type": chat_type},
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                "text": text,
            },
        },
        _fake_bot(),
    )  # type: ignore[return-value]


def _photo_update(
    *,
    user_id: int = 7,
    chat_id: int | None = None,
    chat_type: str = "private",
    voice: bool = False,
    caption: str | None = None,
) -> Update:
    cid = chat_id if chat_id is not None else user_id
    msg: dict[str, Any] = {
        "message_id": 11,
        "date": 1_700_000_000,
        "chat": {"id": cid, "type": chat_type},
        "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
        "photo": [
            {
                "file_id": "photo-file-id",
                "file_unique_id": "AQADTEST",
                "width": 100,
                "height": 100,
                "file_size": 1234,
            }
        ],
    }
    if voice:
        msg["voice"] = {
            "file_id": "voice-file-id",
            "file_unique_id": "AwADTEST",
            "duration": 3,
            "mime_type": "audio/ogg",
            "file_size": 5678,
        }
    if caption is not None:
        msg["caption"] = caption
    return Update.de_json({"update_id": 2, "message": msg}, _fake_bot())  # type: ignore[return-value]


def _callback_update(
    data: str, user_id: int = 7, chat_id: int = 7, message_id: int = 50
) -> Update:
    return Update.de_json(
        {
            "update_id": 3,
            "callback_query": {
                "id": "cq-1",
                "from": {"id": user_id, "is_bot": False, "first_name": "Tester"},
                "chat_instance": "ci-1",
                "data": data,
                "message": {
                    "message_id": message_id,
                    "date": 1_700_000_000,
                    "chat": {"id": chat_id, "type": "private"},
                    "from": {"id": 1, "is_bot": True, "first_name": "Bot"},
                    "text": "earlier",
                },
            },
        },
        _fake_bot(),
    )  # type: ignore[return-value]


# ---------------------------------------------------------------------------
# /start onboarding
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_start_command_in_dm_replies_with_oauth_url(
    mocks: PipelineMocks,
) -> None:
    await expense_pipeline.handle_incoming_message(_text_update("/start", user_id=42))

    assert len(mocks.sent) == 1
    text = mocks.sent[0].text
    # The OAuth URL should be in the reply, pointing at the Splitwise
    # authorize endpoint with a state param.
    assert "secure.splitwise.com/oauth/authorize" in text
    assert "state=" in text
    assert mocks.sent[0].chat_id == 42


@pytest.mark.asyncio
async def test_unknown_dm_text_also_offers_oauth_link(
    mocks: PipelineMocks,
) -> None:
    """An unconnected user pinging the bot gets the OAuth nudge, even if
    the text happens to be a recognised greeting — connection comes first.
    """
    await expense_pipeline.handle_incoming_message(_text_update("hi there", user_id=11))

    assert len(mocks.sent) == 1
    assert "splitwise" in mocks.sent[0].text.lower()
    assert "secure.splitwise.com/oauth/authorize" in mocks.sent[0].text


# ---------------------------------------------------------------------------
# Greeting handler (connected users)
# ---------------------------------------------------------------------------


def test_is_greeting_strips_punctuation_and_whitespace() -> None:
    is_greeting = expense_pipeline._is_greeting
    assert is_greeting("hi") is True
    assert is_greeting("Hi!") is True
    assert is_greeting("  hi.  ") is True
    assert is_greeting("HELLO") is True
    assert is_greeting("hey there") is True
    assert is_greeting("Hey There.") is True
    assert is_greeting("good morning") is True
    assert is_greeting("yo") is True


def test_is_greeting_rejects_anything_with_extra_words_or_empty() -> None:
    is_greeting = expense_pipeline._is_greeting
    # Extra content past the greeting → falls through to parser.
    assert is_greeting("hi I paid $20 at TJ") is False
    assert is_greeting("hello can you help") is False
    # Words that aren't in the greeting set.
    assert is_greeting("ok") is False
    assert is_greeting("split this") is False
    assert is_greeting("") is False
    assert is_greeting("   ") is False


@pytest.mark.asyncio
async def test_greeting_from_connected_user_replies_friendly_and_skips_parser(
    mocks: PipelineMocks,
) -> None:
    """Bare 'hi' from a connected user gets a short friendly reply with
    their first_name and does NOT invoke the parser or create a pending.
    """
    mocks.tokens[42] = ("token", 555)

    await expense_pipeline.handle_incoming_message(_text_update("hi", user_id=42))

    assert len(mocks.sent) == 1
    reply = mocks.sent[0].text
    # Personalised — _text_update sets first_name="Tester".
    assert "Tester" in reply
    # NOT another OAuth link.
    assert "secure.splitwise.com/oauth" not in reply
    # Parser never ran, no pending was created.
    assert mocks.parser_calls == []
    assert mocks.created_pendings == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "greeting", ["hi", "Hi!", "HELLO there", "yo", "  hey  ", "good morning"]
)
async def test_greeting_variants_all_short_circuit(
    mocks: PipelineMocks, greeting: str
) -> None:
    mocks.tokens[42] = ("token", 555)
    await expense_pipeline.handle_incoming_message(_text_update(greeting, user_id=42))
    assert len(mocks.sent) == 1, f"no reply for greeting {greeting!r}"
    assert mocks.parser_calls == [], f"parser ran for greeting {greeting!r}"


@pytest.mark.asyncio
async def test_greeting_with_extra_content_falls_through_to_parser(
    mocks: PipelineMocks,
) -> None:
    """'hi I paid $20...' is NOT a pure greeting — it should reach the
    parser, not the canned greeting reply."""
    mocks.tokens[42] = ("token", 555)

    await expense_pipeline.handle_incoming_message(
        _text_update("hi I paid $20 at TJ split equally with Shreya", user_id=42)
    )

    # Parser ran exactly once. The canned greeting reply was NOT sent.
    assert len(mocks.parser_calls) == 1
    if mocks.sent:
        assert "Hey Tester!" not in mocks.sent[0].text


def test_strip_bot_mentions() -> None:
    """Bot @-mentions are stripped; user @-mentions are untouched."""
    strip = expense_pipeline._strip_bot_mentions
    assert strip("@udhaari_bot yo") == "yo"
    assert strip("yo @udhaari_bot") == "yo"
    assert strip("@udhaari_bot I paid $20") == "I paid $20"
    # Multiple bots in one message (unlikely but defensive).
    assert strip("@some_bot @other_bot hi") == "hi"
    # Plain user mention (no "bot" suffix) — not stripped.
    assert strip("@hardik_username paid") == "@hardik_username paid"
    # Whitespace collapsing.
    assert strip("   @udhaari_bot    yo   there   ") == "yo there"


@pytest.mark.asyncio
async def test_group_atmention_greeting_triggers_friendly_reply(
    mocks: PipelineMocks,
) -> None:
    """``@udhaari_bot yo`` in a group should be treated as a greeting
    (mention stripped first), not fed to the parser."""
    mocks.tokens[42] = ("token", 555)

    await expense_pipeline.handle_incoming_message(
        _text_update(
            "@udhaari_bot yo", user_id=42, chat_id=-1001, chat_type="supergroup"
        )
    )

    assert len(mocks.sent) == 1
    assert "Tester" in mocks.sent[0].text
    assert mocks.parser_calls == []
    # Group: membership got recorded for the sender.
    assert (42, -1001) in mocks.memberships


@pytest.mark.asyncio
async def test_group_atmention_with_expense_text_strips_bot_then_parses(
    mocks: PipelineMocks,
) -> None:
    """``@udhaari_bot I paid $20`` in a group: mention stripped, then
    parser runs (because the residual is not a pure greeting)."""
    mocks.tokens[42] = ("token", 555)

    await expense_pipeline.handle_incoming_message(
        _text_update(
            "@udhaari_bot I paid $20 at TJ",
            user_id=42,
            chat_id=-1001,
            chat_type="supergroup",
        )
    )
    assert len(mocks.parser_calls) == 1
    # The parser sees the stripped text.
    parser_transcript = mocks.parser_calls[0][1]
    assert parser_transcript is not None
    assert "udhaari_bot" not in parser_transcript


@pytest.mark.asyncio
async def test_named_payer_reroutes_pending_to_that_user(
    mocks: PipelineMocks,
) -> None:
    """When the parser identifies a different user as the payer
    ("Hardik paid for dinner"), the pending row's payer_telegram_user_id
    is set to that user — so only THEY can later Confirm."""
    # Prem (sender) and Hardik (named payer) both connected.
    mocks.tokens[42] = ("prem-token", 555)
    mocks.users[42] = User(
        telegram_user_id=42, first_name="Prem", splitwise_user_id=555
    )
    mocks.users[100] = User(
        telegram_user_id=100, first_name="Hardik", splitwise_user_id=900
    )

    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("30.00"),
        currency="USD",
        merchant="Diner",
        split_type="equal",
        # Parser says Hardik paid for an equal 2-way split.
        payer="Hardik",
        splits=[
            Split(name="self", share=0.5),  # self in parser-speak is the payer
            Split(name="Prem", share=0.5),
        ],
        confidence=0.9,
    )

    await expense_pipeline.handle_incoming_message(
        _text_update("Hardik paid $30 at the diner, split with me", user_id=42)
    )

    # Pending row was created keyed to Hardik (id 100), not Prem (id 42).
    assert len(mocks.created_pendings) == 1
    assert mocks.created_pendings[0].payer_telegram_user_id == 100
    # Confirmation message names Hardik as the payer.
    assert len(mocks.sent) == 1
    reply = mocks.sent[0].text
    assert "Paid by: Hardik" in reply
    # "self" placeholder is replaced by the payer's name (Hardik).
    assert "Hardik: 50%" in reply
    # Footer reminds that Hardik is the one who taps Confirm.
    assert "Hardik" in reply.split("\n")[-1]
    assert "Confirm" in reply.split("\n")[-1]


@pytest.mark.asyncio
async def test_named_payer_who_is_unconnected_gets_friendly_error(
    mocks: PipelineMocks,
) -> None:
    """Naming an unconnected user as the payer should fail with a
    helpful message — we can't create an expense on an account that
    doesn't have an OAuth token."""
    mocks.tokens[42] = ("prem-token", 555)
    mocks.users[42] = User(
        telegram_user_id=42, first_name="Prem", splitwise_user_id=555
    )
    # Hardik exists but hasn't OAuth'd (no splitwise_user_id).
    mocks.users[100] = User(
        telegram_user_id=100, first_name="Hardik", splitwise_user_id=None
    )

    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("30.00"),
        currency="USD",
        merchant="Diner",
        split_type="equal",
        payer="Hardik",
        splits=[Split(name="self", share=0.5), Split(name="Prem", share=0.5)],
        confidence=0.9,
    )

    await expense_pipeline.handle_incoming_message(
        _text_update("Hardik paid $30, split with me", user_id=42)
    )

    # No pending created.
    assert mocks.created_pendings == []
    # User-visible error mentions Hardik + /start.
    assert any("Hardik" in m.text and "/start" in m.text for m in mocks.sent)


@pytest.mark.asyncio
async def test_tax_appears_in_confirmation_and_splitwise_details(
    mocks: PipelineMocks,
) -> None:
    """Extracted tax is surfaced in two places: the confirmation message
    (so the user can verify) and the Splitwise expense notes (so the
    receipt audit trail stays intact)."""
    from app.ai.schema import LineItem

    mocks.tokens[42] = ("token", 555)
    mocks.users[42] = User(
        telegram_user_id=42, first_name="Prem", splitwise_user_id=555
    )
    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("21.95"),
        currency="USD",
        merchant="Trader Joe's",
        split_type="equal",
        splits=[Split(name="self", share=1.0)],
        items=[
            LineItem(name="Bananas", price=Decimal("2.50")),
            LineItem(name="Milk", price=Decimal("4.99")),
        ],
        tax=Decimal("1.74"),
        confidence=0.9,
    )

    # Capture
    await expense_pipeline.handle_incoming_message(
        _text_update("I paid $21.95 at TJ", user_id=42)
    )
    assert len(mocks.sent) == 1
    reply = mocks.sent[0].text
    assert "Tax: USD 1.74" in reply

    # Confirm so we can see what gets sent to Splitwise.
    pid = next(iter(mocks.pendings.keys()))
    cb = _callback_update(f"{keyboards.CONFIRM_PREFIX}:{pid.hex}", user_id=42)
    await expense_pipeline.handle_callback(cb.callback_query)  # type: ignore[arg-type]

    assert len(mocks.sw_create_calls) == 1
    details = mocks.sw_create_calls[0].details
    assert details is not None
    assert "Bananas" in details
    assert "Milk" in details
    assert "Tax: USD 1.74" in details


@pytest.mark.asyncio
async def test_sender_can_cancel_even_when_payer_is_someone_else(
    mocks: PipelineMocks,
) -> None:
    """Hardik types 'Shreya paid X' — pending row is keyed to Shreya
    (only she can Confirm), but Hardik should still be able to Cancel
    the message he himself sent."""
    # Both connected.
    mocks.tokens[42] = ("hardik-token", 555)
    mocks.users[42] = User(
        telegram_user_id=42, first_name="Hardik", splitwise_user_id=555
    )
    mocks.users[100] = User(
        telegram_user_id=100, first_name="Shreya", splitwise_user_id=900
    )
    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("30.00"),
        currency="USD",
        merchant="Coffee place",
        split_type="equal",
        payer="Shreya",
        splits=[Split(name="self", share=0.5), Split(name="Hardik", share=0.5)],
        confidence=0.9,
    )

    # Hardik (uid 42) records the expense.
    await expense_pipeline.handle_incoming_message(
        _text_update("Shreya paid $30 split with me", user_id=42)
    )

    # Pending is keyed to Shreya (uid 100), not Hardik.
    assert len(mocks.created_pendings) == 1
    assert mocks.created_pendings[0].payer_telegram_user_id == 100
    # But Hardik's id was stashed in parsed_data so he can still act on it.
    assert mocks.created_pendings[0].parsed_data["_sender_telegram_user_id"] == 42

    # Find the pending UUID we just created.
    pid = next(iter(mocks.pendings.keys()))
    mocks.sent.clear()
    mocks.edits.clear()

    # Hardik (the sender, NOT the payer) taps Cancel → should work.
    cb = _callback_update(f"{keyboards.CANCEL_PREFIX}:{pid.hex}", user_id=42)
    await expense_pipeline.handle_callback(cb.callback_query)  # type: ignore[arg-type]

    assert pid in mocks.deleted_pendings
    assert any("Cancelled" in e.text for e in mocks.edits)


@pytest.mark.asyncio
async def test_sender_still_cannot_confirm_when_payer_is_someone_else(
    mocks: PipelineMocks,
) -> None:
    """Security check: even though the sender can Cancel, only the
    payer can actually authorise the Splitwise expense."""
    mocks.tokens[42] = ("hardik-token", 555)
    mocks.users[42] = User(
        telegram_user_id=42, first_name="Hardik", splitwise_user_id=555
    )
    mocks.users[100] = User(
        telegram_user_id=100, first_name="Shreya", splitwise_user_id=900
    )
    mocks.tokens[100] = ("shreya-token", 900)
    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("30.00"),
        currency="USD",
        merchant="Coffee",
        split_type="equal",
        payer="Shreya",
        splits=[Split(name="self", share=0.5), Split(name="Hardik", share=0.5)],
        confidence=0.9,
    )

    await expense_pipeline.handle_incoming_message(
        _text_update("Shreya paid $30 split with me", user_id=42)
    )
    pid = next(iter(mocks.pendings.keys()))
    mocks.sent.clear()
    mocks.edits.clear()

    # Hardik (sender, NOT payer) taps Confirm → should be rejected.
    cb = _callback_update(f"{keyboards.CONFIRM_PREFIX}:{pid.hex}", user_id=42)
    await expense_pipeline.handle_callback(cb.callback_query)  # type: ignore[arg-type]

    # No Splitwise call was made.
    assert mocks.sw_create_calls == []
    # No completion recorded.
    assert mocks.marked_completed == []
    # Pending row still alive (only confirm or cancel removes it).
    assert pid in mocks.pendings


@pytest.mark.asyncio
async def test_dm_text_capture_resolves_against_connected_user_pool(
    mocks: PipelineMocks,
) -> None:
    """In a DM, 'split with Hardik' should resolve against
    list_connected_users (Hardik may have OAuth'd from a totally
    different chat — there's no group roster to consult)."""
    mocks.tokens[42] = ("token", 555)
    mocks.users[42] = User(
        telegram_user_id=42, first_name="Prem", splitwise_user_id=555
    )
    mocks.users[10] = User(
        telegram_user_id=10, first_name="Hardik", splitwise_user_id=666
    )
    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("20.00"),
        currency="USD",
        merchant="TJ",
        split_type="equal",
        splits=[
            Split(name="self", share=0.5),
            Split(name="Hardik", share=0.5),
        ],
        confidence=0.9,
    )

    await expense_pipeline.handle_incoming_message(
        _text_update("I paid $20 at TJ split equally with Hardik", user_id=42)
    )

    # 1. Parser was told about Hardik as an available split target.
    assert len(mocks.parser_calls) == 1
    ctx = mocks.parser_calls[0][2]
    assert "Hardik" in ctx.member_names

    # 2. The resolved split carries Hardik's real ids, not UNRESOLVED.
    assert len(mocks.created_pendings) == 1
    resolved = mocks.created_pendings[0].parsed_data["_resolved"]
    hardik = next(r for r in resolved if r["name"] == "Hardik")
    assert hardik["telegram_user_id"] == 10
    assert hardik["splitwise_user_id"] == 666
    assert hardik["ambiguous"] is False


# ---------------------------------------------------------------------------
# Photo capture
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_photo_from_unconnected_user_prompts_start_and_skips_pipeline(
    mocks: PipelineMocks,
) -> None:
    # No token configured for user 7 — so the pipeline should bail early.
    await expense_pipeline.handle_incoming_message(_photo_update(user_id=7))

    assert len(mocks.sent) == 1
    assert "/start" in mocks.sent[0].text
    # No parser ran, no pending row, no Splitwise client constructed.
    assert mocks.parser_calls == []
    assert mocks.created_pendings == []
    assert mocks.sw_clients_constructed == []
    assert mocks.downloads == []


@pytest.mark.asyncio
async def test_photo_happy_path_dm(mocks: PipelineMocks) -> None:
    mocks.tokens[7] = ("plain-token", 555)
    mocks.users[7] = User(
        telegram_user_id=7, telegram_username="prem", splitwise_user_id=555
    )

    await expense_pipeline.handle_incoming_message(_photo_update(user_id=7))

    # Parser was invoked with downloaded bytes.
    assert len(mocks.parser_calls) == 1
    image_bytes, transcript, context = mocks.parser_calls[0]
    assert image_bytes.startswith(b"\xff\xd8\xff")
    assert transcript is None  # no voice, no caption
    # DM has no other members.
    assert context.member_names == []
    assert context.payer_name == "prem"

    # Pending row was stored with the parsed JSON + a _resolved sidecar.
    assert len(mocks.created_pendings) == 1
    saved = mocks.created_pendings[0]
    assert saved.payer_telegram_user_id == 7
    assert saved.telegram_group_id is None  # DM
    assert "_resolved" in saved.parsed_data
    assert saved.parsed_data["amount"] in {"47.32", 47.32}

    # Confirmation message went out with the inline keyboard.
    assert len(mocks.sent) == 1
    out = mocks.sent[0]
    assert out.reply_markup is not None
    assert "Trader Joe" in out.text


@pytest.mark.asyncio
async def test_photo_plus_voice_runs_transcribe_then_parse(
    mocks: PipelineMocks,
) -> None:
    mocks.tokens[7] = ("tok", 555)
    mocks.users[7] = User(
        telegram_user_id=7, telegram_username="prem", splitwise_user_id=555
    )
    mocks.transcript_return = "split this with Priya"

    await expense_pipeline.handle_incoming_message(_photo_update(user_id=7, voice=True))

    # Both photo and voice were downloaded (in either order).
    assert sorted(mocks.downloads) == ["photo-file-id", "voice-file-id"]
    # Whisper ran on the voice bytes.
    assert len(mocks.transcribed) == 1
    assert mocks.transcribed[0][1] == "audio/ogg"
    # The parser received the transcript.
    _img, transcript, _ctx = mocks.parser_calls[0]
    assert transcript == "split this with Priya"


@pytest.mark.asyncio
async def test_photo_with_caption_uses_caption_when_no_voice(
    mocks: PipelineMocks,
) -> None:
    mocks.tokens[7] = ("tok", 555)
    mocks.users[7] = User(
        telegram_user_id=7, telegram_username="prem", splitwise_user_id=555
    )

    await expense_pipeline.handle_incoming_message(
        _photo_update(user_id=7, caption="split with Priya 60/40")
    )

    _img, transcript, _ctx = mocks.parser_calls[0]
    assert transcript == "split with Priya 60/40"


@pytest.mark.asyncio
async def test_group_photo_records_membership_and_uses_roster(
    mocks: PipelineMocks,
) -> None:
    mocks.tokens[7] = ("tok", 555)
    mocks.users[7] = User(
        telegram_user_id=7, telegram_username="prem", splitwise_user_id=555
    )
    mocks.group_members[-100] = [
        mocks.users[7],
        User(
            telegram_user_id=8,
            telegram_username="priya",
            splitwise_user_id=666,
        ),
    ]

    await expense_pipeline.handle_incoming_message(
        _photo_update(user_id=7, chat_id=-100, chat_type="supergroup")
    )

    # Membership recorded for the sender + group.
    assert (7, -100) in mocks.memberships
    # The parser got the group roster (priya, not "prem" since that's "self").
    _img, _t, ctx = mocks.parser_calls[0]
    assert ctx.member_names == ["priya"]


@pytest.mark.asyncio
async def test_group_text_only_message_records_membership_but_stays_quiet(
    mocks: PipelineMocks,
) -> None:
    """Privacy mode means we mostly see @-mentions; we still want the roster."""
    await expense_pipeline.handle_incoming_message(
        _text_update("hi", user_id=7, chat_type="supergroup")
    )

    assert mocks.memberships == [(7, 7)]
    # No reply (groups stay quiet on plain text we don't act on).
    assert mocks.sent == []


# ---------------------------------------------------------------------------
# Confirm / cancel / edit callbacks
# ---------------------------------------------------------------------------


async def _seed_pending(mocks: PipelineMocks, *, payer_id: int = 7) -> Any:
    """Helper: run the capture pipeline once so a pending row exists."""
    mocks.tokens[payer_id] = ("tok", 555)
    mocks.users[payer_id] = User(
        telegram_user_id=payer_id, telegram_username="prem", splitwise_user_id=555
    )
    mocks.group_members[payer_id] = [
        mocks.users[payer_id],
        User(telegram_user_id=8, telegram_username="priya", splitwise_user_id=666),
    ]
    # We run in DM mode so the resolution doesn't depend on group roster.
    # But the parser already names "Priya" — give her a fake row so the
    # resolver finds her via group lookup. The capture in DM mode skips
    # the group roster, so we set splits to ["self", "self"] instead by
    # patching parsed_expense.
    mocks.parsed_expense = ParsedExpense(
        amount=Decimal("20.00"),
        currency="USD",
        merchant="Bodega",
        split_type="equal",
        splits=[
            Split(name="self", share=0.5),
            Split(name="priya", share=0.5),
        ],
        confidence=0.9,
    )

    # Run capture in a group context so the roster is consulted.
    update = _photo_update(user_id=payer_id, chat_id=-77, chat_type="supergroup")
    mocks.group_members[-77] = [
        mocks.users[payer_id],
        User(telegram_user_id=8, telegram_username="priya", splitwise_user_id=666),
    ]
    await expense_pipeline.handle_incoming_message(update)
    # Grab the pending id from the most recent insert.
    assert mocks.created_pendings, "capture didn't create a pending row"
    # The pending id is whichever uuid is in m.pendings.
    pid = next(iter(mocks.pendings))
    return pid


@pytest.mark.asyncio
async def test_confirm_tap_creates_splitwise_expense(mocks: PipelineMocks) -> None:
    pid = await _seed_pending(mocks)
    # Reset captures from the seed step we don't want polluting assertions.
    mocks.sent.clear()
    mocks.edits.clear()

    cb_update = _callback_update(
        data=f"{keyboards.CONFIRM_PREFIX}:{pid.hex}", user_id=7
    )
    await expense_pipeline.handle_callback(cb_update.callback_query)  # type: ignore[arg-type]

    # Splitwise client got constructed with the user's token,
    # create_expense was called with balanced splits, and mark_completed
    # was invoked with the returned expense id.
    assert mocks.sw_clients_constructed == ["tok"]
    assert len(mocks.sw_create_calls) == 1
    call = mocks.sw_create_calls[0]
    assert call.cost == Decimal("20.00")
    assert call.currency == "USD"
    assert call.description == "Bodega"
    ids = {s.splitwise_user_id for s in call.splits}
    assert ids == {555, 666}
    # mark_completed was called.
    assert mocks.marked_completed == [(pid, mocks.sw_create_return)]
    # The message was edited to the success line.
    assert len(mocks.edits) == 1
    assert "Added to Splitwise" in mocks.edits[0].text


@pytest.mark.asyncio
async def test_cancel_tap_deletes_pending_and_edits_message(
    mocks: PipelineMocks,
) -> None:
    pid = await _seed_pending(mocks)
    mocks.sent.clear()
    mocks.edits.clear()

    cb_update = _callback_update(data=f"{keyboards.CANCEL_PREFIX}:{pid.hex}", user_id=7)
    await expense_pipeline.handle_callback(cb_update.callback_query)  # type: ignore[arg-type]

    assert mocks.deleted_pendings == [pid]
    assert mocks.sw_create_calls == []
    assert len(mocks.edits) == 1
    assert "Cancelled" in mocks.edits[0].text


@pytest.mark.asyncio
async def test_confirm_on_missing_pending_replies_friendly_no_crash(
    mocks: PipelineMocks,
) -> None:
    # No seeding — the UUID won't resolve.
    bogus = uuid4()
    cb_update = _callback_update(
        data=f"{keyboards.CONFIRM_PREFIX}:{bogus.hex}", user_id=7
    )
    await expense_pipeline.handle_callback(cb_update.callback_query)  # type: ignore[arg-type]

    # No Splitwise call, no completion, but the user was told what happened.
    assert mocks.sw_create_calls == []
    assert mocks.marked_completed == []
    assert len(mocks.edits) == 1
    assert "already handled" in mocks.edits[0].text.lower() or (
        "expired" in mocks.edits[0].text.lower()
    )


@pytest.mark.asyncio
async def test_confirm_by_non_payer_is_rejected(
    mocks: PipelineMocks,
) -> None:
    pid = await _seed_pending(mocks, payer_id=7)
    mocks.sent.clear()
    mocks.edits.clear()

    cb_update = _callback_update(
        data=f"{keyboards.CONFIRM_PREFIX}:{pid.hex}", user_id=999
    )
    await expense_pipeline.handle_callback(cb_update.callback_query)  # type: ignore[arg-type]

    # No expense was created; the original message was NOT edited.
    assert mocks.sw_create_calls == []
    assert mocks.edits == []
    # A side reply to the non-payer.
    assert any("only the person" in s.text.lower() for s in mocks.sent)


@pytest.mark.asyncio
async def test_callback_with_unknown_prefix_is_ignored(
    mocks: PipelineMocks,
) -> None:
    cb_update = _callback_update(data="xx:deadbeef", user_id=7)
    await expense_pipeline.handle_callback(cb_update.callback_query)  # type: ignore[arg-type]

    assert mocks.sent == []
    assert mocks.edits == []


@pytest.mark.asyncio
async def test_callback_with_bad_uuid_is_ignored(mocks: PipelineMocks) -> None:
    cb_update = _callback_update(data=f"{keyboards.CONFIRM_PREFIX}:nothex", user_id=7)
    await expense_pipeline.handle_callback(cb_update.callback_query)  # type: ignore[arg-type]

    assert mocks.sent == []
    assert mocks.edits == []
