"""Gemini 2.0 Flash receipt parser (Brick D).

Implements :class:`app.ai.provider.ExpenseParser` using the new unified
``google-genai`` SDK. The SDK exposes both sync and async surfaces; we
use the async one (``client.aio.models.generate_content``) so we don't
need ``asyncio.to_thread``.

Privacy: this module never logs receipt bytes or full LLM output. Only
metadata and (on failure) short, redacted excerpts.
"""

from __future__ import annotations

import logging
from copy import deepcopy
from typing import Any

from google import genai
from google.genai import types as genai_types
from pydantic import ValidationError

from app.ai.prompts import SYSTEM_PROMPT, STRICT_RETRY_SUFFIX, build_user_prompt
from app.ai.provider import ExpenseParser, GroupContext, ParserError
from app.ai.schema import ParsedExpense
from app.config import settings

# JSON Schema keys that Gemini's ``Schema`` model rejects. We strip these
# from ``ParsedExpense.model_json_schema()`` before handing it to Gemini.
# Our Pydantic model still enforces them on the *response* — only the
# transport-level schema we send to Gemini drops them.
_GEMINI_DISALLOWED_KEYS = frozenset({"exclusiveMinimum", "exclusiveMaximum", "$schema"})


def _inline_refs(schema: Any, defs: dict[str, Any]) -> Any:
    """Recursively replace ``{"$ref": "#/$defs/Name"}`` with the named schema.

    Gemini's schema validator doesn't follow ``$ref``; we resolve them
    locally so nested models (e.g. ``Split`` inside ``ParsedExpense``)
    end up inlined.
    """
    if isinstance(schema, dict):
        ref = schema.get("$ref")
        if isinstance(ref, str) and ref.startswith("#/$defs/"):
            name = ref.removeprefix("#/$defs/")
            return _inline_refs(deepcopy(defs.get(name, {})), defs)
        return {k: _inline_refs(v, defs) for k, v in schema.items()}
    if isinstance(schema, list):
        return [_inline_refs(item, defs) for item in schema]
    return schema


def _strip_disallowed(schema: Any) -> Any:
    """Recursively drop any key in :data:`_GEMINI_DISALLOWED_KEYS`."""
    if isinstance(schema, dict):
        return {
            k: _strip_disallowed(v)
            for k, v in schema.items()
            if k not in _GEMINI_DISALLOWED_KEYS
        }
    if isinstance(schema, list):
        return [_strip_disallowed(item) for item in schema]
    return schema


def _gemini_compatible_schema(json_schema: dict[str, Any]) -> dict[str, Any]:
    """Transform a Pydantic JSON Schema into something Gemini accepts.

    Two changes:
        * Inline every ``$ref`` against the top-level ``$defs`` block.
        * Strip JSON Schema keys Gemini's transport-side ``Schema``
          validator rejects (``exclusiveMinimum``, ``exclusiveMaximum``,
          ``$schema``).

    Returns a new dict; the input is not mutated.
    """
    defs = json_schema.get("$defs", {})
    inlined = _inline_refs(deepcopy(json_schema), defs)
    if isinstance(inlined, dict):
        inlined.pop("$defs", None)
    return _strip_disallowed(inlined)


_LOG = logging.getLogger(__name__)

# Model id is intentionally hard-coded here, not in settings, because
# swapping to a different Gemini model is a deploy-level decision, not a
# config knob.
GEMINI_MODEL = "gemini-2.0-flash"

# Default mime for inline image data. Telegram delivers JPEG-encoded
# photos, but PNGs from a manual upload are common too — Gemini accepts
# both behind the same mime "image/jpeg"/"image/png".
_DEFAULT_IMAGE_MIME = "image/jpeg"


def _detect_image_mime(image_bytes: bytes) -> str:
    """Cheap magic-byte sniff. Falls back to JPEG.

    We intentionally do not pull in Pillow just for this — the two
    formats Telegram realistically delivers (JPEG and PNG) are both
    trivially identifiable by their first few bytes.
    """
    if image_bytes.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if image_bytes.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if image_bytes.startswith(b"GIF87a") or image_bytes.startswith(b"GIF89a"):
        return "image/gif"
    if image_bytes[:4] == b"RIFF" and image_bytes[8:12] == b"WEBP":
        return "image/webp"
    return _DEFAULT_IMAGE_MIME


class GeminiExpenseParser(ExpenseParser):
    """Vision-grounded receipt parser backed by Gemini 2.0 Flash.

    Args:
        client: A pre-built ``google.genai.Client``. If ``None`` we build
            one from ``settings.gemini_api_key``. Tests pass a fake.
        model: Override the Gemini model id (defaults to
            ``gemini-2.0-flash``).
    """

    def __init__(
        self,
        client: genai.Client | None = None,
        model: str = GEMINI_MODEL,
    ) -> None:
        self._model = model
        self._client = client or _build_default_client()

    async def parse(
        self,
        image_bytes: bytes,
        transcript: str | None,
        context: GroupContext,
    ) -> ParsedExpense:
        if not image_bytes:
            raise ParserError("image_bytes is empty")

        user_text = build_user_prompt(transcript, context)
        image_part = genai_types.Part.from_bytes(
            data=image_bytes, mime_type=_detect_image_mime(image_bytes)
        )

        # First attempt — structured-output mode.
        raw = await self._generate(user_text, image_part, strict_retry=False)
        parsed = _try_validate(raw)
        if parsed is not None:
            return parsed

        # Second (and final) attempt — same content + a stricter nudge.
        _LOG.warning(
            "gemini.parse.retry strict=true reason=invalid_json model=%s", self._model
        )
        raw = await self._generate(user_text, image_part, strict_retry=True)
        parsed = _try_validate(raw)
        if parsed is not None:
            return parsed

        raise ParserError("Gemini did not return a valid ParsedExpense after one retry")

    async def _generate(
        self,
        user_text: str,
        image_part: genai_types.Part,
        *,
        strict_retry: bool,
    ) -> str:
        """Issue one Gemini call; return the raw text response."""
        system_instruction = SYSTEM_PROMPT + (
            STRICT_RETRY_SUFFIX if strict_retry else ""
        )
        config = genai_types.GenerateContentConfig(
            system_instruction=system_instruction,
            response_mime_type="application/json",
            response_schema=_gemini_compatible_schema(
                ParsedExpense.model_json_schema()
            ),
            temperature=0.2,
        )
        response = await self._client.aio.models.generate_content(
            model=self._model,
            contents=[image_part, user_text],
            config=config,
        )
        text = _extract_text(response)
        if not text:
            raise ParserError("Gemini returned an empty response")
        return text


def _extract_text(response: Any) -> str:
    """Pull the text payload out of a Gemini response.

    The SDK exposes a convenience ``.text`` attribute on the response.
    We fall back to walking ``candidates`` for robustness if that's
    ever ``None``.
    """
    text = getattr(response, "text", None)
    if text:
        return text
    candidates = getattr(response, "candidates", None) or []
    for cand in candidates:
        content = getattr(cand, "content", None)
        for part in getattr(content, "parts", None) or []:
            piece = getattr(part, "text", None)
            if piece:
                return piece
    return ""


def _try_validate(raw: str) -> ParsedExpense | None:
    """Validate ``raw`` against the schema; return ``None`` on failure."""
    try:
        return ParsedExpense.model_validate_json(raw)
    except ValidationError as exc:
        # Log only error metadata, never the full raw text — receipt
        # content is sensitive.
        _LOG.warning("gemini.parse.validation_failed errors=%d", len(exc.errors()))
        return None
    except ValueError as exc:
        _LOG.warning("gemini.parse.invalid_json type=%s", type(exc).__name__)
        return None


def _build_default_client() -> genai.Client:
    """Construct a Gemini client from settings, validating the key is set."""
    api_key = settings.gemini_api_key.get_secret_value()
    if not api_key:
        raise ParserError(
            "GEMINI_API_KEY is not set; cannot build default GeminiExpenseParser"
        )
    return genai.Client(api_key=api_key)
