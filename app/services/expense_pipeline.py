"""Brick F — orchestrator.

Glues Bricks B (Telegram), C (Splitwise), D (AI parsing), and E
(persistence) into a single end-to-end flow:

    photo (+ optional voice/caption) → parse → confirmation message →
    confirm tap → Splitwise.createExpense → "Added to Splitwise ✓".

This module is a leaf: nothing in the rest of the app imports from it.
The Telegram webhook (Brick B) calls :func:`handle_incoming_message` and
:func:`handle_callback`; everything else happens here.

Privacy:
    Never log Splitwise tokens, OAuth codes, image bytes, voice bytes,
    or the transcript text. ``telegram_user_id`` and pending UUIDs are
    fine.
"""

from __future__ import annotations

import logging
import re
from contextvars import ContextVar
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID, uuid4

from telegram import CallbackQuery, Update

from app.ai import (
    ParsedExpense,
    ParserError,
    TranscriptionError,
    default_parser,
    transcribe_voice,
)
from app.db.expenses import (
    PendingNotFoundError,
    create_pending,
    delete_pending,
    get_pending,
    mark_completed,
)
from app.db.groups import record_membership
from app.db.known_people import list_known_people
from app.db.models import User
from app.db.users import get_user, upsert_user
from app.observability import (
    is_enabled,
    mark_conversation_end,
    observe,
    record_error,
    trace_context,
    update_span,
)
from app.services.group_context import (
    ResolvedSplit,
    UNRESOLVED,
    build_group_context,
    eligible_split_members,
    find_user_by_name,
    resolve_split_names,
)
from app.services.known_people_sync import sync_known_people
from app.splitwise import (
    Split as SwSplit,
    SplitwiseAPIError,
    SplitwiseClient,
    build_auth_url,
    load_user_token,
    mint_state,
)
from app.telegram import bot as _tg_bot
from app.telegram import files as _tg_files
from app.telegram import keyboards

log = logging.getLogger(__name__)


# In groups with privacy mode ON, the bot only sees messages that @-mention
# it (or reply to it). Those messages arrive with "@udhaari_bot" embedded
# in the text, which trips simple matchers — "@udhaari_bot yo" wouldn't
# match the greeting handler because the literal text isn't "yo".
# Telegram bot usernames are required to end in "bot", so we strip any
# @<word>bot mention from message text/caption before downstream matching.
_BOT_MENTION_RE = re.compile(r"@\w+bot\b", re.IGNORECASE)


def _strip_bot_mentions(text: str) -> str:
    """Remove ``@<botname>`` mentions and collapse whitespace.

    Safe to call on any user-supplied text — it only matches handles
    ending in ``bot`` (per Telegram's bot-username rule), so regular
    user @-mentions like ``@hardik_username`` are untouched.
    """
    cleaned = _BOT_MENTION_RE.sub("", text)
    return " ".join(cleaned.split())


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------


async def _capture_user_identity(user: Any) -> None:
    """Upsert the sender's identity columns from a Telegram ``User``.

    Best-effort: failures are logged and swallowed so a flaky DB write
    never blocks dispatch. We feed first_name + last_name + username
    here so the orchestrator's name resolver can match natural-language
    references in voice notes ("split with Shreya") to the right row.
    """
    if user is None:
        return
    try:
        await upsert_user(
            telegram_user_id=user.id,
            telegram_username=user.username,
            first_name=user.first_name,
            last_name=user.last_name,
        )
    except Exception:  # noqa: BLE001 — best-effort; never crash dispatch
        log.exception("upsert_user (identity capture) failed user_id=%s", user.id)


# Per-request conversation id. Minted at the top of ``handle_incoming_message``
# and read deep in ``_finalize_capture`` (to make it the pending expense's id)
# without threading it through every capture function. Set per webhook task, so
# it's request-isolated. ``None`` outside a request (e.g. the TTL sweep).
_conversation_id: ContextVar[UUID | None] = ContextVar("conversation_id", default=None)


def _conversation_session_id(conversation_id: UUID) -> str:
    """The Langfuse session key for a conversation — single source of truth.

    A whole add-an-expense journey (message → confirm/cancel tap) shares this
    key because the tap carries the same id in its ``callback_data``; single-
    turn conversations (greeting, ``/start``) are their own session.
    """
    return f"conv-{conversation_id.hex}"


@observe(name="incoming_message", capture_input=False, capture_output=False)
async def handle_incoming_message(update: Update) -> None:
    """Top-level message router (DMs and groups, not callbacks).

    Decision tree:
        * Photo (with or without voice/caption)        → photo capture pipeline.
        * Text starting with ``/start``                → OAuth onboarding (handles
                                                         already-connected case too).
        * Voice or text from a CONNECTED user          → text-only capture (parser
                                                         runs without an image; low
                                                         confidence asks for more).
        * Anything from an UNCONNECTED user in a DM    → friendly OAuth nudge.
        * Anything from an UNCONNECTED user in a group → stay quiet (don't spam).
    """
    message = update.message
    if message is None:
        return

    user = update.effective_user
    chat = update.effective_chat
    if user is None or chat is None:
        # No way to act without a sender or chat; this should be
        # impossible for messages we care about.
        return

    is_private = chat.type == "private"
    telegram_user_id = user.id
    chat_id = chat.id

    # Mint the conversation id up front so it can be the trace's session key
    # immediately AND become the pending expense's row id later (set via the
    # context var, read in _finalize_capture). No placeholder, no update.
    conversation_id = uuid4()
    _conversation_id.set(conversation_id)

    # Build tag list before opening the trace context — we want these
    # attributes to propagate to every child span (parse, transcribe,
    # create_expense). The configured ``settings.environment`` is added
    # automatically inside ``trace_context``.
    extra_tags: list[str] = ["dm" if is_private else "group"]
    if message.photo:
        extra_tags.append("photo")
    if message.voice is not None:
        extra_tags.append("voice")
    if message.text:
        extra_tags.append("text")

    with trace_context(
        user_id=str(telegram_user_id),
        session_id=_conversation_session_id(conversation_id),
        tags=extra_tags,
        metadata={
            "chat_type": chat.type,
            "first_name": user.first_name or "",
        },
    ):
        update_span(
            input={
                "telegram_user_id": telegram_user_id,
                "chat_id": chat_id,
                "chat_type": chat.type,
                "first_name": user.first_name,
                "has_photo": bool(message.photo),
                "has_voice": message.voice is not None,
                # A photo's text lives in ``caption``, not ``text`` — count
                # whichever is present so photo captions aren't reported as 0.
                "text_len": len(message.text or message.caption or ""),
            },
        )

        # Capture identity on every message so the name resolver can
        # match natural-language references later.
        await _capture_user_identity(user)

        # Always remember the sender exists in this group, regardless
        # of whether the message itself triggers a capture flow.
        if not is_private:
            try:
                await record_membership(telegram_user_id, chat_id)
            except Exception:  # noqa: BLE001 — best-effort; never crash dispatch
                log.exception(
                    "record_membership failed for telegram_user_id=%s chat=%s",
                    telegram_user_id,
                    chat_id,
                )

        # Photo (+ optional voice/caption) drives the photo capture pipeline.
        if message.photo:
            await _process_expense_capture(update)
            return

        text = _strip_bot_mentions(message.text or "")

        # ``/start`` is special: it's the onboarding entry point. Handle
        # it before any token check so unconnected users see the OAuth
        # link and connected users get a friendly "you're already in".
        if text.startswith("/start"):
            await _handle_start_command(telegram_user_id, chat_id)
            return

        # Anything else requires a connected user. Look up once and branch.
        token_record = await load_user_token(telegram_user_id)
        if token_record is None:
            if is_private:
                await _handle_unconnected_dm(telegram_user_id, chat_id)
            # Group: deliberately silent so we don't expose someone's
            # disconnected state in front of everyone.
            return

        # Pure greeting ("hi", "yo", "hello there") → friendly reply,
        # skip the parser entirely.
        if text and _is_greeting(text):
            await _handle_greeting(chat_id, user.first_name)
            return

        # Voice without a photo → transcribe → text-only capture.
        if message.voice is not None:
            await _process_voice_only_capture(
                update, voice=message.voice, token_record=token_record
            )
            return

        # Text without a photo → try text-only capture.
        if text:
            await _process_text_only_capture(
                update, text_input=text, token_record=token_record
            )
            return

        # Empty / non-text / non-media message — stay quiet.


@observe(name="callback", capture_input=False, capture_output=False)
async def handle_callback(callback_query: CallbackQuery) -> None:
    """Dispatch an inline-keyboard tap based on its ``callback_data`` prefix.

    Always answers the callback (clears the spinner) before doing the
    real work — Telegram only gives the bot ~5s before the client gives
    up on the loading state.
    """
    data = callback_query.data or ""
    user = callback_query.from_user
    if user is None:
        log.warning("callback without from_user: data=%r", data)
        return
    telegram_user_id = user.id
    callback_chat = callback_query.message.chat if callback_query.message else None
    callback_chat_id = callback_chat.id if callback_chat is not None else None

    # callback_data shape: "<prefix>:<uuid_hex>". Parse the id up front so the
    # tap lands in the same Langfuse session as the message that created the
    # pending expense (the conversation), rather than a fresh per-chat bucket.
    prefix, _, hex_id = data.partition(":")
    pending_id: UUID | None = None
    if hex_id:
        try:
            pending_id = UUID(hex=hex_id)
        except ValueError:
            pending_id = None
    session_id = (
        _conversation_session_id(pending_id)
        if pending_id is not None
        else (f"chat-{callback_chat_id}" if callback_chat_id else None)
    )

    with trace_context(
        user_id=str(telegram_user_id),
        session_id=session_id,
        tags=["callback", prefix or "unknown"],
    ):
        update_span(
            input={
                "telegram_user_id": telegram_user_id,
                "callback_data": data,
                "first_name": user.first_name,
            },
        )

        # Refresh identity columns on every interaction.
        await _capture_user_identity(user)

        # Best-effort spinner clear; never let this kill the handler.
        try:
            await callback_query.answer()
        except Exception:  # noqa: BLE001
            log.exception("callback_query.answer() failed")

        # Anything without a valid "<prefix>:<uuid_hex>" id is ignored.
        if pending_id is None:
            log.info("callback ignored: unparseable data=%r", data)
            return

        message = callback_query.message
        chat_id = message.chat_id if message is not None else None
        message_id = message.message_id if message is not None else None

        if prefix == keyboards.CONFIRM_PREFIX:
            await handle_confirm(pending_id, telegram_user_id, chat_id, message_id)
        elif prefix == keyboards.CANCEL_PREFIX:
            await handle_cancel(pending_id, telegram_user_id, chat_id, message_id)
        else:
            log.info("callback ignored: unknown prefix=%r", prefix)


# ---------------------------------------------------------------------------
# Onboarding helpers
# ---------------------------------------------------------------------------

# Sent when the user gives us so little to work with that we can't even
# guess at an expense (low parser confidence, empty transcript, etc.).
_NOT_ENOUGH_INFO_HINT = (
    "I didn't get enough to record an expense. Try something like:\n"
    '  • "I paid $24.50 at Trader Joe\'s, split equally with Shreya"\n'
    '  • "Dinner was 60 dollars, split 2:1 with Hardik"\n'
    "Or send a photo of the receipt and I'll read it for you."
)

# Floor for parser confidence on text-only / voice-only captures. Below
# this we don't bother the user with a confirmation — we ask them to add
# more detail instead.
_TEXT_CAPTURE_CONFIDENCE_FLOOR = 0.3

# Standalone greetings we recognise. Match against a normalised (lowercase,
# alphanumerics only, single-spaced) version of the user's text, so "Hi!",
# "Hi", "hi.", and "Hi " all reduce to "hi". Anything longer or with extra
# words ("hi i paid $20") falls through to the parser — we only short-
# circuit on *pure* greetings.
_GREETINGS = frozenset(
    {
        "hi",
        "hello",
        "hey",
        "yo",
        "yoyo",
        "yo yo",
        "sup",
        "wassup",
        "whats up",
        "whatup",
        "hola",
        "namaste",
        "howdy",
        "hiya",
        "heya",
        "morning",
        "good morning",
        "afternoon",
        "good afternoon",
        "evening",
        "good evening",
        "hi there",
        "hey there",
        "hello there",
    }
)


def _is_greeting(text: str) -> bool:
    """True when ``text`` is one of the pure-greeting phrases in :data:`_GREETINGS`.

    Strips punctuation and collapses whitespace, so "Hi!", "  hi.  ", and
    "Hi" all match. Anything that contains extra words beyond the greeting
    (e.g. "hi I paid $20") will not match — those go to the parser.
    """
    cleaned = "".join(c for c in text.lower() if c.isalnum() or c.isspace())
    normalised = " ".join(cleaned.split())
    return bool(normalised) and normalised in _GREETINGS


async def _handle_greeting(chat_id: int, first_name: str | None) -> None:
    """Reply to a pure greeting (hi/yo/hello/etc.) with a short prompt.

    Uses the user's first_name when we have it so the reply feels personal
    rather than canned.
    """
    name = first_name.strip() if first_name else ""
    salutation = f"Hey {name}! 👋" if name else "Hey! 👋"
    await _safe_send(
        chat_id,
        f"{salutation}\n\n"
        "Send me a receipt photo, or describe an expense in text — I'll "
        "add it to Splitwise.\n\n"
        'Example: "I paid $24 at Trader Joe\'s, split equally with Shreya"',
    )
    mark_conversation_end("single_turn")


async def _build_oauth_url(telegram_user_id: int, chat_id: int) -> str | None:
    """Mint a state token and return the user's personalised OAuth URL.

    Returns ``None`` after sending a user-visible error if Splitwise
    credentials aren't configured (the orchestrator can't recover).
    """
    try:
        return build_auth_url(mint_state(telegram_user_id))
    except RuntimeError as exc:
        log.exception(
            "build_auth_url failed for telegram_user_id=%s: %s",
            telegram_user_id,
            exc,
        )
        await _safe_send(
            chat_id,
            "Something is misconfigured on my side — please ping the bot owner.",
        )
        return None


async def _sync_known_people_safe(telegram_user_id: int, plain_token: str) -> None:
    """Best-effort refresh of a user's Splitwise friends cache. Never raises."""
    try:
        await sync_known_people(telegram_user_id, plain_token)
    except Exception:  # noqa: BLE001 — contacts sync must never break a flow
        log.exception("known_people sync failed telegram_user_id=%s", telegram_user_id)


async def _handle_start_command(telegram_user_id: int, chat_id: int) -> None:
    """Handle ``/start`` for both unconnected and already-connected users.

    Unconnected → welcome + OAuth link.
    Connected   → friendly "you're already in, here's what to do" message.
    """
    token_record = await load_user_token(telegram_user_id)
    if token_record is not None:
        # Already authenticated. Don't send another OAuth link — that's
        # the bug this commit fixes. Refresh their Splitwise friends cache
        # (best-effort) so /start doubles as "re-sync my contacts".
        await _sync_known_people_safe(telegram_user_id, token_record[0])
        await _safe_send(
            chat_id,
            "You're already connected to Splitwise. ✓\n\n"
            "Send me a receipt photo, or describe the expense in text "
            '(e.g. "I paid $24 at Trader Joe\'s, split with Shreya").',
        )
        mark_conversation_end("already_connected")
        return

    url = await _build_oauth_url(telegram_user_id, chat_id)
    if url is None:
        return
    await _safe_send(
        chat_id,
        "Welcome! I'm udhaari — I add expenses to Splitwise for you.\n\n"
        "Step 1: connect your Splitwise account using the link below.\n"
        "Step 2: send a receipt photo, or describe the expense in text, "
        "and I'll do the rest.\n\n"
        f"Connect Splitwise: {url}",
    )
    mark_conversation_end("onboarding_link_sent")


async def _handle_unconnected_dm(telegram_user_id: int, chat_id: int) -> None:
    """A DM from someone who hasn't OAuth'd yet — send them the link."""
    url = await _build_oauth_url(telegram_user_id, chat_id)
    if url is None:
        return
    await _safe_send(
        chat_id,
        "Hi! DM me a receipt photo (or describe the expense in text) and "
        "I'll add it to Splitwise.\n"
        f"First, connect your Splitwise account: {url}",
    )


# ---------------------------------------------------------------------------
# Capture pipeline
# ---------------------------------------------------------------------------


async def _process_expense_capture(update: Update) -> None:
    """Photo → parse → store pending → post confirmation.

    Errors at any step are surfaced as a short user-visible message and
    swallowed so the webhook still returns 200.
    """
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    # Caller (handle_incoming_message) guarantees these are non-None and
    # that the message has at least one photo; assert defensively anyway
    # for type-checkers.
    assert message is not None and message.photo
    assert user is not None and chat is not None

    telegram_user_id = user.id
    chat_id = chat.id
    is_private = chat.type == "private"
    telegram_group_id = None if is_private else chat_id

    # 1. Token / connection check.
    token_record = await load_user_token(telegram_user_id)
    if token_record is None:
        log.info(
            "capture: unconnected user telegram_user_id=%s chat=%s",
            telegram_user_id,
            chat_id,
        )
        await _safe_send(
            chat_id,
            "I don't see a Splitwise connection for you yet. DM me /start "
            "and I'll send you a link to connect.",
        )
        return
    _payer_token, payer_splitwise_user_id = token_record

    # 2. Build context for the parser.
    try:
        context = await build_group_context(telegram_group_id, telegram_user_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "build_group_context failed telegram_user_id=%s", telegram_user_id
        )
        record_error("build_group_context failed")
        await _safe_send(chat_id, "Something went wrong loading your group. Try again?")
        return

    # 3. Download photo + (optional) voice.
    photo = message.photo[-1]  # largest size last
    try:
        image_bytes = await _tg_files.download_telegram_file(photo.file_id)
    except Exception:  # noqa: BLE001
        log.exception("download photo failed telegram_user_id=%s", telegram_user_id)
        record_error("photo download failed")
        await _safe_send(
            chat_id, "I couldn't fetch your photo from Telegram. Try again?"
        )
        return

    transcript: str | None = None
    voice = message.voice
    if voice is not None:
        try:
            audio_bytes = await _tg_files.download_telegram_file(voice.file_id)
            transcript = await transcribe_voice(
                audio_bytes, voice.mime_type or "audio/ogg"
            )
        except TranscriptionError:
            log.warning("transcribe_voice failed telegram_user_id=%s", telegram_user_id)
            # Don't block the capture: a missing transcript just means the
            # parser does its best from the photo + caption alone.
            transcript = None
        except Exception:  # noqa: BLE001
            log.exception("download voice failed telegram_user_id=%s", telegram_user_id)
            transcript = None

    # Use the photo caption as a fallback / supplement when there's no
    # voice transcript (Telegram lets users add a text caption to a photo).
    # Strip @-mentions of the bot — they're noise to the parser.
    if not transcript and message.caption:
        transcript = _strip_bot_mentions(message.caption) or None

    # 4. Parse with Brick D.
    try:
        parsed = await default_parser().parse(image_bytes, transcript, context)
    except ParserError:
        log.warning(
            "parser failed telegram_user_id=%s chat=%s",
            telegram_user_id,
            chat_id,
        )
        await _safe_send(
            chat_id,
            "I couldn't read that receipt clearly. Try a sharper photo?",
        )
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "parser raised telegram_user_id=%s chat=%s",
            telegram_user_id,
            chat_id,
        )
        record_error("parser raised")
        await _safe_send(chat_id, "Something went wrong parsing the receipt.")
        return

    # 5-7. Common: resolve payer + splits, persist, post confirmation.
    await _finalize_capture(
        parsed=parsed,
        message=message,
        sender_telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        telegram_group_id=telegram_group_id,
    )


async def _process_text_only_capture(
    update: Update,
    *,
    text_input: str,
    token_record: tuple[str, int],
) -> None:
    """Capture an expense from text alone (no photo).

    Used for two entry points:
      * DM text from a connected user.
      * @-mention text in a group from a connected user (no photo, no voice).

    Low parser confidence triggers a "not enough info" reply instead of
    a confirmation — we don't want to fabricate an expense from "hi".
    """
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    assert message is not None and user is not None and chat is not None

    telegram_user_id = user.id
    chat_id = chat.id
    is_private = chat.type == "private"
    telegram_group_id = None if is_private else chat_id
    _payer_token, payer_splitwise_user_id = token_record

    try:
        context = await build_group_context(telegram_group_id, telegram_user_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "build_group_context failed telegram_user_id=%s", telegram_user_id
        )
        await _safe_send(chat_id, "Something went wrong loading your group. Try again?")
        return

    try:
        parsed = await default_parser().parse(b"", text_input, context)
    except ParserError:
        log.info(
            "text-only parser gave up telegram_user_id=%s chat=%s",
            telegram_user_id,
            chat_id,
        )
        await _safe_send(chat_id, _NOT_ENOUGH_INFO_HINT)
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "text-only parser raised telegram_user_id=%s chat=%s",
            telegram_user_id,
            chat_id,
        )
        await _safe_send(chat_id, "Something went wrong parsing that.")
        return

    if parsed.confidence < _TEXT_CAPTURE_CONFIDENCE_FLOOR:
        log.info(
            "text-only low confidence telegram_user_id=%s confidence=%.2f",
            telegram_user_id,
            parsed.confidence,
        )
        await _safe_send(chat_id, _NOT_ENOUGH_INFO_HINT)
        mark_conversation_end("single_turn")
        return

    await _finalize_capture(
        parsed=parsed,
        message=message,
        sender_telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        telegram_group_id=telegram_group_id,
    )


async def _process_voice_only_capture(
    update: Update,
    *,
    voice: Any,
    token_record: tuple[str, int],
) -> None:
    """Voice without photo → Whisper → text-only capture path."""
    message = update.message
    user = update.effective_user
    chat = update.effective_chat
    assert message is not None and user is not None and chat is not None

    chat_id = chat.id
    telegram_user_id = user.id

    try:
        audio_bytes = await _tg_files.download_telegram_file(voice.file_id)
    except Exception:  # noqa: BLE001
        log.exception("download voice failed telegram_user_id=%s", telegram_user_id)
        await _safe_send(chat_id, "Couldn't fetch your voice note. Try again?")
        return

    try:
        transcript = await transcribe_voice(audio_bytes, voice.mime_type or "audio/ogg")
    except TranscriptionError:
        log.warning("transcribe_voice failed telegram_user_id=%s", telegram_user_id)
        await _safe_send(
            chat_id, "I couldn't transcribe that. Try again, or type the details."
        )
        return
    except Exception:  # noqa: BLE001
        log.exception("transcribe_voice raised telegram_user_id=%s", telegram_user_id)
        await _safe_send(chat_id, "Something went wrong with your voice note.")
        return

    if not transcript.strip():
        await _safe_send(chat_id, _NOT_ENOUGH_INFO_HINT)
        return

    await _process_text_only_capture(
        update, text_input=transcript, token_record=token_record
    )


def _user_display_name(user: User) -> str:
    """Friendly name for a user — what we show in confirmation messages."""
    return user.first_name or user.telegram_username or f"user_{user.telegram_user_id}"


async def _resolve_payer(
    parsed_payer: str | None,
    sender_telegram_user_id: int,
    members: list[User],
) -> User | None:
    """Determine the actual payer of an expense.

    Rules:
      * ``parsed_payer`` is None / empty / "self" → the sender is the payer.
      * Otherwise we match the name against the eligible-member pool. If
        it matches the sender's own name, that's still the sender. If it
        matches a different connected user, that user is the payer.

    Returns the resolved ``User`` or ``None`` if the name is unmatchable
    or ambiguous. ``None`` for the sender path means we couldn't even
    find their own User row, which is a configuration problem.
    """
    sender_norm = ""
    sender_user: User | None = None
    for m in members:
        if m.telegram_user_id == sender_telegram_user_id:
            sender_user = m
            break
    if sender_user is None:
        sender_user = await get_user(sender_telegram_user_id)

    parsed_norm = (parsed_payer or "").strip().lower()
    if not parsed_payer or parsed_norm in ("", "self"):
        return sender_user

    # If the user named themselves (e.g. by first name), treat as self.
    if sender_user is not None:
        for cand in (
            sender_user.first_name,
            sender_user.last_name,
            sender_user.telegram_username,
        ):
            if cand:
                sender_norm = cand.strip().lower()
                if sender_norm and sender_norm == parsed_norm:
                    return sender_user

    # Otherwise look for a non-sender member who matches.
    other_members = [
        m for m in members if m.telegram_user_id != sender_telegram_user_id
    ]
    return find_user_by_name(parsed_payer, other_members)


async def _finalize_capture(
    *,
    parsed: ParsedExpense,
    message: Any,
    sender_telegram_user_id: int,
    chat_id: int,
    telegram_group_id: int | None,
) -> None:
    """Common tail of every capture flow: resolve payer → resolve splits →
    persist → confirm.

    Whether the parse came from a photo, voice transcript, or typed text,
    the steps from here on are identical: figure out who actually paid
    (the sender by default, but anyone the user named explicitly), match
    split names to members, save a pending row keyed to the payer, and
    post the inline-keyboard confirmation.
    """
    members_for_resolution = await eligible_split_members(telegram_group_id)

    # 1. Resolve the payer. If the user said "Hardik paid", we re-route
    # the whole expense to Hardik's Splitwise account; only HE can confirm.
    payer_user = await _resolve_payer(
        parsed.payer, sender_telegram_user_id, members_for_resolution
    )
    if payer_user is None:
        log.info(
            "payer unresolved sender=%s parsed_payer=%r",
            sender_telegram_user_id,
            parsed.payer,
        )
        if parsed.payer:
            await _safe_send(
                chat_id,
                f"I couldn't find '{parsed.payer}' here. Cancel and try "
                "again with a clearer name, or have them DM me /start first.",
            )
        else:
            await _safe_send(
                chat_id,
                "I couldn't find your account — try DMing me /start to reconnect.",
            )
        return

    if payer_user.splitwise_user_id is None:
        name = _user_display_name(payer_user)
        if payer_user.telegram_user_id == sender_telegram_user_id:
            await _safe_send(
                chat_id,
                "You haven't connected Splitwise yet — DM me /start to fix that.",
            )
        else:
            await _safe_send(
                chat_id,
                f"{name} hasn't connected Splitwise yet. Ask them to DM me "
                "/start first, then re-send the receipt.",
            )
        return

    payer_telegram_user_id = payer_user.telegram_user_id
    payer_splitwise_user_id = payer_user.splitwise_user_id
    payer_display = _user_display_name(payer_user)

    # 2. Resolve split names against the eligible roster (using the payer's
    # IDs so "self" maps to whoever actually paid, not necessarily sender).
    # Fall back to the payer's cached Splitwise friends for names that aren't
    # connected bot members ("split with Cody" where Cody never joined).
    # Best-effort: if the cache table is missing (migration not yet run) or
    # the DB hiccups, degrade to members-only rather than failing the capture.
    try:
        known_people = await list_known_people(payer_telegram_user_id)
    except Exception:  # noqa: BLE001
        log.exception(
            "list_known_people failed payer=%s; resolving members-only",
            payer_telegram_user_id,
        )
        known_people = []
    resolved = resolve_split_names(
        parsed.splits,
        members_for_resolution,
        payer_telegram_user_id=payer_telegram_user_id,
        payer_splitwise_user_id=payer_splitwise_user_id,
        known_people=known_people,
    )

    # 3. Persist a pending row keyed to the PAYER (only they can Confirm
    # since the expense will hit their Splitwise account). We also stash
    # the sender in parsed_data so they can Cancel/Edit — the "event
    # owner" intuition: if Hardik types "Shreya paid X", Hardik should
    # still be able to retract the message he just sent.
    parsed_data = parsed.model_dump(mode="json")
    parsed_data["_resolved"] = [
        {
            "name": r.name,
            "share": r.share,
            "telegram_user_id": r.telegram_user_id,
            "splitwise_user_id": r.splitwise_user_id,
            "ambiguous": r.ambiguous,
            "external": r.external,
        }
        for r in resolved
    ]
    parsed_data["_sender_telegram_user_id"] = sender_telegram_user_id

    try:
        pending = await create_pending(
            # Adopt the conversation id as the row id so the message trace
            # and the later confirm/cancel tap share one Langfuse session.
            id=_conversation_id.get(),
            telegram_message_id=message.message_id,
            telegram_group_id=telegram_group_id,
            payer_telegram_user_id=payer_telegram_user_id,
            parsed_data=parsed_data,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "create_pending failed sender=%s payer=%s",
            sender_telegram_user_id,
            payer_telegram_user_id,
        )
        await _safe_send(chat_id, "I couldn't save the draft. Try again?")
        return

    # 4. Post confirmation message.
    text = _format_confirmation_text(parsed, resolved, payer_display=payer_display)
    try:
        await _tg_bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboards.confirmation_keyboard(pending.id),
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "send confirmation failed pending=%s sender=%s",
            pending.id,
            sender_telegram_user_id,
        )

    log.info(
        "capture.ok pending=%s sender=%s payer=%s chat=%s",
        pending.id,
        sender_telegram_user_id,
        payer_telegram_user_id,
        chat_id,
    )


def _format_expense_details(parsed: ParsedExpense) -> str | None:
    """Build the free-form ``details`` note that goes on the Splitwise expense.

    Bullet list of line items off the receipt, with a final "Tax: ..."
    line when a tax amount was extracted. Returns ``None`` when nothing
    useful is available (text-only captures, or receipts where Gemini
    couldn't read either items or tax). Splitwise displays this as
    expense notes — visible to everyone the expense is split with.
    """
    if not parsed.items and parsed.tax is None:
        return None
    lines: list[str] = []
    for item in parsed.items:
        qty = f"{item.quantity}× " if item.quantity and item.quantity > 1 else ""
        # Render price with 2 decimal places; Decimal preserves precision
        # but might render as e.g. "2.5" without quantising.
        price = f"{item.price:.2f}"
        lines.append(f"• {qty}{item.name} {parsed.currency} {price}")
    if parsed.tax is not None:
        lines.append(f"Tax: {parsed.currency} {parsed.tax:.2f}")
    return "\n".join(lines) if lines else None


def _sender_id_from_pending(pending: Any) -> int | None:
    """Read the sender's telegram_user_id stashed in ``parsed_data``.

    Returns ``None`` if the row predates the sender-stash convention
    (e.g. a long-pending row from before the feature shipped) or the
    payload is malformed.
    """
    data = pending.parsed_data or {}
    sender = data.get("_sender_telegram_user_id")
    if isinstance(sender, int):
        return sender
    return None


def _is_pending_owner(pending: Any, telegram_user_id: int) -> bool:
    """True if ``telegram_user_id`` can Cancel ``pending``.

    Either the payer (whose Splitwise account is on the line) or the
    sender (whose message kicked the whole thing off) can retract.
    Confirm stays strict — only the payer can authorise the actual
    expense (see :func:`handle_confirm`).
    """
    if pending.payer_telegram_user_id == telegram_user_id:
        return True
    sender = _sender_id_from_pending(pending)
    return sender is not None and sender == telegram_user_id


def _format_confirmation_text(
    parsed: ParsedExpense,
    resolved: list[ResolvedSplit],
    *,
    payer_display: str,
) -> str:
    """Build the short confirmation summary shown alongside the buttons.

    Kept deliberately compact — Telegram clients clip long messages and
    the keyboard rows below are what the user is here for. The "self"
    placeholder in splits is replaced by the resolved payer's name so
    the reader sees "Hardik: 50%" rather than "self: 50%".
    """
    lines: list[str] = []
    merchant = parsed.merchant or "(unknown merchant)"
    lines.append(f"{merchant} — {parsed.amount} {parsed.currency}")
    if parsed.receipt_date is not None:
        lines.append(f"Date: {parsed.receipt_date.isoformat()}")
    if parsed.tax is not None:
        lines.append(f"Tax: {parsed.currency} {parsed.tax:.2f}")
    lines.append(f"Paid by: {payer_display}")
    if parsed.split_type != "equal":
        lines.append(f"Split type: {parsed.split_type}")

    for r in resolved:
        pct = f"{r.share * 100:.0f}%"
        display_name = payer_display if r.name.lower() == "self" else r.name
        if r.ambiguous:
            label = f"{display_name} (ambiguous — please verify)"
        elif r.external:
            label = f"{display_name} (Splitwise friend)"
        elif r.telegram_user_id == UNRESOLVED:
            label = f"{display_name} (not in this group yet)"
        else:
            label = display_name
        lines.append(f"  • {label}: {pct}")
    lines.append(
        f"\n{payer_display}, tap Confirm to add to Splitwise. "
        "Anyone here can Cancel."
    )
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Callback handlers
# ---------------------------------------------------------------------------


async def handle_confirm(
    pending_id: UUID,
    telegram_user_id: int,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    """Confirm tap: load pending → POST to Splitwise → mark completed."""
    pending = await get_pending(pending_id)
    if pending is None:
        await _edit_or_send(
            chat_id,
            message_id,
            "This expense was already handled or has expired.",
        )
        return

    # Idempotency: if a different user tapped the keyboard, refuse.
    if (
        pending.payer_telegram_user_id is not None
        and pending.payer_telegram_user_id != telegram_user_id
    ):
        log.info(
            "confirm rejected: tapper=%s != payer=%s pending=%s",
            telegram_user_id,
            pending.payer_telegram_user_id,
            pending_id,
        )
        # Don't edit the message; just send a side reply so the original
        # sender can still confirm.
        if chat_id is not None:
            await _safe_send(
                chat_id, "Only the person who sent the receipt can confirm it."
            )
        return

    token_record = await load_user_token(telegram_user_id)
    if token_record is None:
        await _edit_or_send(
            chat_id,
            message_id,
            "You're not connected to Splitwise. DM me /start.",
        )
        return
    plain_token, _payer_swid = token_record

    # Re-hydrate the parsed expense + resolved splits from the pending row.
    parsed_data = pending.parsed_data or {}
    resolved_meta: list[dict[str, object]] = list(
        parsed_data.get("_resolved", []) or []
    )

    try:
        parsed = ParsedExpense.model_validate(
            {k: v for k, v in parsed_data.items() if k != "_resolved"}
        )
    except Exception:  # noqa: BLE001
        log.exception("confirm: parsed_data invalid pending=%s", pending_id)
        await _edit_or_send(
            chat_id, message_id, "The draft for this expense was corrupted."
        )
        return

    sw_splits = _build_sw_splits(parsed, resolved_meta)
    if not sw_splits:
        await _edit_or_send(
            chat_id,
            message_id,
            "I couldn't match all participants to Splitwise users. Ask them to "
            "DM me /start first, then resend.",
        )
        return

    client = SplitwiseClient(plain_token)
    description = parsed.merchant or "Expense"
    details = _format_expense_details(parsed)
    try:
        sw_expense_id = await client.create_expense(
            cost=parsed.amount,
            currency=parsed.currency,
            description=description,
            group_id=None,
            splits=sw_splits,
            details=details,
            expense_date=parsed.receipt_date,
        )
    except SplitwiseAPIError:
        log.warning(
            "splitwise create_expense failed pending=%s telegram_user_id=%s",
            pending_id,
            telegram_user_id,
        )
        await _edit_or_send(
            chat_id,
            message_id,
            "Splitwise rejected the expense. Try again, or add it in the app.",
        )
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "splitwise create_expense raised pending=%s telegram_user_id=%s",
            pending_id,
            telegram_user_id,
        )
        await _edit_or_send(
            chat_id,
            message_id,
            "Couldn't reach Splitwise right now. Try again in a minute.",
        )
        return

    try:
        await mark_completed(pending_id, sw_expense_id)
    except PendingNotFoundError:
        # Highly unlikely (we just read it), but a sweep could fire between
        # get_pending and mark_completed. The expense is already in
        # Splitwise; we just can't reflect it back.
        log.warning(
            "pending vanished before mark_completed pending=%s sw_expense=%s",
            pending_id,
            sw_expense_id,
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "mark_completed failed pending=%s sw_expense=%s",
            pending_id,
            sw_expense_id,
        )

    await _edit_or_send(chat_id, message_id, "Added to Splitwise ✓")
    mark_conversation_end("expense_created")
    log.info(
        "confirm.ok pending=%s sw_expense=%s telegram_user_id=%s",
        pending_id,
        sw_expense_id,
        telegram_user_id,
    )


async def handle_cancel(
    pending_id: UUID,
    telegram_user_id: int,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    """Cancel tap: delete the pending row and edit the message.

    Either the payer or the original sender can Cancel — the sender
    "owns" the message they sent and should be able to retract it even
    when the parsed payer is someone else.
    """
    pending = await get_pending(pending_id)
    if pending is None:
        await _edit_or_send(
            chat_id, message_id, "This expense was already handled or has expired."
        )
        return
    if not _is_pending_owner(pending, telegram_user_id):
        if chat_id is not None:
            await _safe_send(
                chat_id,
                "Only the payer or whoever sent this message can cancel it.",
            )
        return

    try:
        await delete_pending(pending_id)
    except Exception:  # noqa: BLE001
        log.exception("delete_pending failed pending=%s", pending_id)
        # Fall through and still tell the user it's cancelled — TTL
        # will eventually clean it up.

    await _edit_or_send(chat_id, message_id, "Cancelled.")
    mark_conversation_end("cancelled")
    log.info("cancel.ok pending=%s telegram_user_id=%s", pending_id, telegram_user_id)


@observe(name="expense_abandoned", capture_input=False, capture_output=False)
async def _emit_abandoned(conversation_id: UUID) -> None:
    """Emit one terminal 'abandoned' trace into a conversation's session."""
    with trace_context(
        session_id=_conversation_session_id(conversation_id),
        tags=["abandoned"],
    ):
        mark_conversation_end("abandoned")


async def mark_conversations_abandoned(conversation_ids: list[UUID]) -> None:
    """Mark expired-without-a-tap conversations as abandoned in Langfuse.

    Called by the TTL sweep with the ids it just deleted. A pending that
    reaches the sweep was never confirmed or cancelled (those delete it
    first), so it's genuinely abandoned. No-op when Langfuse is disabled.
    """
    if not is_enabled() or not conversation_ids:
        return
    for conversation_id in conversation_ids:
        try:
            await _emit_abandoned(conversation_id)
        except Exception:  # noqa: BLE001 — observability must not break the sweep
            log.exception("failed to mark conversation abandoned id=%s", conversation_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _build_sw_splits(
    parsed: ParsedExpense, resolved_meta: list[dict[str, object]]
) -> list[SwSplit]:
    """Convert resolved splits into balanced :class:`Split` objects.

    Splitwise requires each user's ``paid_share`` and ``owed_share`` to
    sum to the expense total. We assume the payer paid the full amount
    (the common case for "I bought this, split it with X"); per-user
    paid_share allocations are a Phase 6 concern.

    Returns an empty list if any participant doesn't have a
    ``splitwise_user_id`` (i.e. they haven't connected) or is marked as
    :data:`UNRESOLVED` — the caller should ask the user to resolve.
    """
    if not resolved_meta:
        return []

    total = Decimal(str(parsed.amount))
    # Build a tentative list and bail if any participant is unusable.
    tentative: list[tuple[int, float]] = []
    for meta in resolved_meta:
        sw_id = meta.get("splitwise_user_id")
        if not isinstance(sw_id, int) or sw_id == 0:
            return []
        share = meta.get("share")
        if not isinstance(share, int | float):
            return []
        tentative.append((sw_id, float(share)))

    # Compute owed_share per user as ``total * share`` rounded to cents.
    cents = (total * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP)
    raw_owed: list[Decimal] = []
    for _sw_id, share in tentative:
        sub = (cents * Decimal(str(share))).quantize(
            Decimal("1"), rounding=ROUND_HALF_UP
        )
        raw_owed.append(sub)

    # Reconcile rounding drift: ensure sum(raw_owed) == cents exactly.
    drift = cents - sum(raw_owed, Decimal(0))
    if drift != 0 and raw_owed:
        # Apply the drift to the first participant (typically the payer
        # since "self" is usually emitted first).
        raw_owed[0] += drift

    splits: list[SwSplit] = []
    payer_sw_id = tentative[0][0]
    for (sw_id, _share), owed_cents in zip(tentative, raw_owed, strict=True):
        owed = (owed_cents / Decimal(100)).quantize(Decimal("0.01"))
        paid = total if sw_id == payer_sw_id else Decimal("0.00")
        splits.append(
            SwSplit(
                splitwise_user_id=sw_id,
                paid_share=paid,
                owed_share=owed,
            )
        )
    return splits


async def _safe_send(chat_id: int, text: str) -> None:
    """``send_message`` that swallows transport errors.

    Used in error paths where we can't usefully recover from a failed
    Telegram round-trip — the user just won't see our message.
    """
    try:
        await _tg_bot.send_message(chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001
        log.exception("send_message failed chat=%s", chat_id)


async def _edit_or_send(
    chat_id: int | None,
    message_id: int | None,
    text: str,
) -> None:
    """Try to edit the original confirmation; fall back to a new message.

    The keyboard is stripped on edit so the user can't tap a stale button.
    """
    if chat_id is None or message_id is None:
        return
    try:
        await _tg_bot.edit_message(
            chat_id=chat_id, message_id=message_id, text=text, reply_markup=None
        )
        return
    except Exception:  # noqa: BLE001
        log.exception(
            "edit_message failed chat=%s message=%s; falling back to send",
            chat_id,
            message_id,
        )
    await _safe_send(chat_id, text)


__all__ = [
    "handle_callback",
    "handle_cancel",
    "handle_confirm",
    "handle_incoming_message",
]
