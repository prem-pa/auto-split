"""Groq Whisper voice transcription (Brick D).

The Groq Python SDK is synchronous, so we wrap the call in
``asyncio.to_thread`` to keep our public surface awaitable. v1 forces
``language="en"`` — multilingual support is a later concern.
"""

from __future__ import annotations

import asyncio
import logging
from functools import lru_cache

from groq import Groq

from app.config import settings

_LOG = logging.getLogger(__name__)

# Groq's fastest currently-available Whisper variant.
WHISPER_MODEL = "whisper-large-v3-turbo"
WHISPER_LANGUAGE = "en"

# Mime → filename suffix. Whisper inspects the filename to pick a
# decoder, so we send a sensible extension for the bytes we have.
_MIME_TO_SUFFIX = {
    "audio/ogg": "ogg",
    "audio/ogg; codecs=opus": "ogg",
    "audio/oga": "oga",
    "audio/opus": "opus",
    "audio/mpeg": "mp3",
    "audio/mp3": "mp3",
    "audio/mp4": "m4a",
    "audio/m4a": "m4a",
    "audio/x-m4a": "m4a",
    "audio/wav": "wav",
    "audio/x-wav": "wav",
    "audio/webm": "webm",
    "audio/flac": "flac",
}


class TranscriptionError(RuntimeError):
    """Raised when transcription fails or returns nothing usable."""


def _filename_for_mime(mime: str) -> str:
    """Best-effort filename so Whisper picks the right decoder."""
    suffix = _MIME_TO_SUFFIX.get(mime.lower().split(";")[0].strip(), "ogg")
    return f"voice.{suffix}"


@lru_cache(maxsize=1)
def _default_client() -> Groq:
    """Build a Groq client once from settings."""
    api_key = settings.groq_api_key.get_secret_value()
    if not api_key:
        raise TranscriptionError(
            "GROQ_API_KEY is not set; cannot transcribe voice messages"
        )
    return Groq(api_key=api_key)


async def transcribe_voice(
    audio: bytes,
    mime: str,
    *,
    client: Groq | None = None,
) -> str:
    """Transcribe an audio clip to English text via Groq Whisper.

    Args:
        audio: Raw audio bytes (Telegram delivers OGG/Opus for voice
            notes; we accept other common formats too).
        mime: The audio mime type. Used to pick a filename suffix Whisper
            can recognise.
        client: Override the Groq client (tests pass a fake).

    Returns:
        The transcribed text. Empty string if the clip had no speech.

    Raises:
        TranscriptionError: If the API call fails or returns no usable
            transcript.
    """
    if not audio:
        raise TranscriptionError("audio bytes are empty")

    groq_client = client or _default_client()
    filename = _filename_for_mime(mime)

    def _call() -> str:
        # Groq accepts a (filename, bytes, content_type) tuple for the
        # ``file`` parameter; the filename helps the model pick a
        # decoder.
        result = groq_client.audio.transcriptions.create(
            file=(filename, audio, mime),
            model=WHISPER_MODEL,
            language=WHISPER_LANGUAGE,
            response_format="json",
        )
        # The SDK returns a typed object with a ``.text`` attribute, but
        # we also tolerate a plain dict / string for forward-compat.
        text = getattr(result, "text", None)
        if text is None and isinstance(result, dict):
            text = result.get("text")
        if text is None and isinstance(result, str):
            text = result
        return (text or "").strip()

    try:
        transcript = await asyncio.to_thread(_call)
    except Exception as exc:  # noqa: BLE001 — surface as a typed error
        _LOG.warning("groq.transcribe.failed type=%s mime=%s", type(exc).__name__, mime)
        raise TranscriptionError(f"Groq transcription failed: {exc}") from exc

    # Length only — never log the transcript content itself, which may
    # contain personal info ("split with my therapist...").
    _LOG.info("groq.transcribe.ok mime=%s chars=%d", mime, len(transcript))
    return transcript
