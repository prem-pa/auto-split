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
from decimal import ROUND_HALF_UP, Decimal
from typing import Any
from uuid import UUID

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
from app.db.users import upsert_user
from app.services.group_context import (
    ResolvedSplit,
    UNRESOLVED,
    build_group_context,
    eligible_split_members,
    resolve_split_names,
)
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

    # Capture identity (first_name / last_name / username) on every message
    # so the name resolver can match natural-language references later.
    await _capture_user_identity(user)

    # Always remember the sender exists in this group, regardless of
    # whether the message itself triggers a capture flow.
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

    # ``/start`` is special: it's the onboarding entry point. Handle it
    # before any token check so unconnected users see the OAuth link and
    # connected users get a friendly "you're already in".
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

    # Pure greeting ("hi", "yo", "hello there") → friendly reply, skip the
    # parser entirely. Must come before voice/text capture so "hi" doesn't
    # get fed to Gemini and come back as a "not enough info" template.
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

    # Refresh identity columns on every interaction.
    await _capture_user_identity(user)

    # Best-effort spinner clear; never let this kill the handler.
    try:
        await callback_query.answer()
    except Exception:  # noqa: BLE001
        log.exception("callback_query.answer() failed")

    # callback_data shape: "<prefix>:<uuid_hex>". Anything else is
    # ignored.
    if ":" not in data:
        log.info("callback ignored: unparseable data=%r", data)
        return
    prefix, _, hex_id = data.partition(":")
    try:
        pending_id = UUID(hex=hex_id)
    except ValueError:
        log.info("callback ignored: bad uuid hex=%r", hex_id)
        return

    message = callback_query.message
    chat_id = message.chat_id if message is not None else None
    message_id = message.message_id if message is not None else None

    if prefix == keyboards.CONFIRM_PREFIX:
        await handle_confirm(pending_id, telegram_user_id, chat_id, message_id)
    elif prefix == keyboards.CANCEL_PREFIX:
        await handle_cancel(pending_id, telegram_user_id, chat_id, message_id)
    elif prefix == keyboards.EDIT_PREFIX:
        await handle_edit(pending_id, telegram_user_id, chat_id, message_id)
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


async def _handle_start_command(telegram_user_id: int, chat_id: int) -> None:
    """Handle ``/start`` for both unconnected and already-connected users.

    Unconnected → welcome + OAuth link.
    Connected   → friendly "you're already in, here's what to do" message.
    """
    token_record = await load_user_token(telegram_user_id)
    if token_record is not None:
        # Already authenticated. Don't send another OAuth link — that's
        # the bug this commit fixes.
        await _safe_send(
            chat_id,
            "You're already connected to Splitwise. ✓\n\n"
            "Send me a receipt photo, or describe the expense in text "
            '(e.g. "I paid $24 at Trader Joe\'s, split with Shreya").',
        )
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
        await _safe_send(chat_id, "Something went wrong loading your group. Try again?")
        return

    # 3. Download photo + (optional) voice.
    photo = message.photo[-1]  # largest size last
    try:
        image_bytes = await _tg_files.download_telegram_file(photo.file_id)
    except Exception:  # noqa: BLE001
        log.exception("download photo failed telegram_user_id=%s", telegram_user_id)
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
        await _safe_send(chat_id, "Something went wrong parsing the receipt.")
        return

    # 5-7. Common: resolve, persist, post confirmation.
    await _finalize_capture(
        parsed=parsed,
        message=message,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        telegram_group_id=telegram_group_id,
        payer_splitwise_user_id=payer_splitwise_user_id,
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
        return

    await _finalize_capture(
        parsed=parsed,
        message=message,
        telegram_user_id=telegram_user_id,
        chat_id=chat_id,
        telegram_group_id=telegram_group_id,
        payer_splitwise_user_id=payer_splitwise_user_id,
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


async def _finalize_capture(
    *,
    parsed: ParsedExpense,
    message: Any,
    telegram_user_id: int,
    chat_id: int,
    telegram_group_id: int | None,
    payer_splitwise_user_id: int,
) -> None:
    """Common tail of every capture flow: resolve → persist → confirm.

    Whether the parse came from a photo, voice transcript, or typed text,
    the steps from here on are identical: match names to group members,
    save a pending row, and post the inline-keyboard confirmation.
    """
    # 1. Resolve split names against the eligible roster (same rules
    # build_group_context used when prompting the parser — see
    # eligible_split_members docstring).
    members_for_resolution = await eligible_split_members(telegram_group_id)
    resolved = resolve_split_names(
        parsed.splits,
        members_for_resolution,
        payer_telegram_user_id=telegram_user_id,
        payer_splitwise_user_id=payer_splitwise_user_id,
    )

    # 2. Persist a pending row. Pydantic JSON mode so Decimal serialises
    # as a number (Supabase JSONB accepts that).
    parsed_data = parsed.model_dump(mode="json")
    parsed_data["_resolved"] = [
        {
            "name": r.name,
            "share": r.share,
            "telegram_user_id": r.telegram_user_id,
            "splitwise_user_id": r.splitwise_user_id,
            "ambiguous": r.ambiguous,
        }
        for r in resolved
    ]

    try:
        pending = await create_pending(
            telegram_message_id=message.message_id,
            telegram_group_id=telegram_group_id,
            payer_telegram_user_id=telegram_user_id,
            parsed_data=parsed_data,
        )
    except Exception:  # noqa: BLE001
        log.exception("create_pending failed telegram_user_id=%s", telegram_user_id)
        await _safe_send(chat_id, "I couldn't save the draft. Try again?")
        return

    # 3. Post confirmation message.
    text = _format_confirmation_text(parsed, resolved)
    try:
        await _tg_bot.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboards.confirmation_keyboard(pending.id),
        )
    except Exception:  # noqa: BLE001
        log.exception(
            "send confirmation failed pending=%s telegram_user_id=%s",
            pending.id,
            telegram_user_id,
        )

    log.info(
        "capture.ok pending=%s telegram_user_id=%s chat=%s",
        pending.id,
        telegram_user_id,
        chat_id,
    )


def _format_confirmation_text(
    parsed: ParsedExpense, resolved: list[ResolvedSplit]
) -> str:
    """Build the short confirmation summary shown alongside the buttons.

    Kept deliberately compact — Telegram clients clip long messages and
    the keyboard rows below are what the user is here for.
    """
    lines: list[str] = []
    merchant = parsed.merchant or "(unknown merchant)"
    lines.append(f"{merchant} — {parsed.amount} {parsed.currency}")
    if parsed.split_type != "equal":
        lines.append(f"Split type: {parsed.split_type}")

    for r in resolved:
        pct = f"{r.share * 100:.0f}%"
        if r.ambiguous:
            label = f"{r.name} (ambiguous — please verify)"
        elif r.telegram_user_id == UNRESOLVED:
            label = f"{r.name} (not in this group yet)"
        else:
            label = r.name
        lines.append(f"  • {label}: {pct}")
    lines.append("\nConfirm to add to Splitwise.")
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
    try:
        sw_expense_id = await client.create_expense(
            cost=parsed.amount,
            currency=parsed.currency,
            description=description,
            group_id=None,
            splits=sw_splits,
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
    """Cancel tap: delete the pending row and edit the message."""
    pending = await get_pending(pending_id)
    if pending is None:
        await _edit_or_send(
            chat_id, message_id, "This expense was already handled or has expired."
        )
        return
    if (
        pending.payer_telegram_user_id is not None
        and pending.payer_telegram_user_id != telegram_user_id
    ):
        if chat_id is not None:
            await _safe_send(
                chat_id, "Only the person who sent the receipt can cancel it."
            )
        return

    try:
        await delete_pending(pending_id)
    except Exception:  # noqa: BLE001
        log.exception("delete_pending failed pending=%s", pending_id)
        # Fall through and still tell the user it's cancelled — TTL
        # will eventually clean it up.

    await _edit_or_send(chat_id, message_id, "Cancelled.")
    log.info("cancel.ok pending=%s telegram_user_id=%s", pending_id, telegram_user_id)


async def handle_edit(
    pending_id: UUID,
    telegram_user_id: int,
    chat_id: int | None,
    message_id: int | None,
) -> None:
    """Edit tap: tell the user to resend; pending is left for TTL.

    v1 is intentionally dumb — per the handoff doc, per-field edit is a
    Phase 6 concern. We don't delete the pending row so the user has a
    window to come back and confirm if they change their mind.
    """
    pending = await get_pending(pending_id)
    if pending is None:
        await _edit_or_send(
            chat_id, message_id, "This expense was already handled or has expired."
        )
        return
    if (
        pending.payer_telegram_user_id is not None
        and pending.payer_telegram_user_id != telegram_user_id
    ):
        if chat_id is not None:
            await _safe_send(
                chat_id, "Only the person who sent the receipt can edit it."
            )
        return

    await _edit_or_send(
        chat_id,
        message_id,
        "Send the receipt again with what you'd like to change.",
    )
    log.info("edit.prompt pending=%s telegram_user_id=%s", pending_id, telegram_user_id)


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
    "handle_edit",
    "handle_incoming_message",
]
