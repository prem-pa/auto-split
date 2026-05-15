"""Tests for the Groq Whisper transcription wrapper."""

from __future__ import annotations

from dataclasses import dataclass

import pytest

from app.ai.transcribe import (
    WHISPER_LANGUAGE,
    WHISPER_MODEL,
    TranscriptionError,
    transcribe_voice,
)


@dataclass
class _FakeTranscription:
    text: str


class _FakeTranscriptions:
    def __init__(self, text: str = "split this with priya") -> None:
        self._text = text
        self.calls: list[dict] = []
        self.error: Exception | None = None

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _FakeTranscription(text=self._text)


class _FakeAudio:
    def __init__(self, text: str = "split this with priya") -> None:
        self.transcriptions = _FakeTranscriptions(text)


class _FakeGroq:
    def __init__(self, text: str = "split this with priya") -> None:
        self.audio = _FakeAudio(text)


@pytest.mark.asyncio
async def test_transcribe_voice_passes_english_and_mime() -> None:
    fake = _FakeGroq("split with priya 50/50")
    result = await transcribe_voice(b"oggdata", "audio/ogg", client=fake)  # type: ignore[arg-type]
    assert result == "split with priya 50/50"

    calls = fake.audio.transcriptions.calls
    assert len(calls) == 1
    call = calls[0]
    assert call["model"] == WHISPER_MODEL
    assert call["language"] == WHISPER_LANGUAGE == "en"

    # ``file`` is a (filename, bytes, mime) tuple.
    filename, payload, content_type = call["file"]
    assert payload == b"oggdata"
    assert content_type == "audio/ogg"
    assert filename.endswith(".ogg")


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "mime,expected_ext",
    [
        ("audio/ogg", "ogg"),
        ("audio/ogg; codecs=opus", "ogg"),
        ("audio/mpeg", "mp3"),
        ("audio/mp4", "m4a"),
        ("audio/wav", "wav"),
        ("audio/webm", "webm"),
        ("audio/unknown", "ogg"),  # fallback
    ],
)
async def test_transcribe_picks_filename_suffix_from_mime(
    mime: str, expected_ext: str
) -> None:
    fake = _FakeGroq("hi")
    await transcribe_voice(b"x", mime, client=fake)  # type: ignore[arg-type]
    filename = fake.audio.transcriptions.calls[0]["file"][0]
    assert filename == f"voice.{expected_ext}"


@pytest.mark.asyncio
async def test_transcribe_empty_audio_raises() -> None:
    fake = _FakeGroq()
    with pytest.raises(TranscriptionError):
        await transcribe_voice(b"", "audio/ogg", client=fake)  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_transcribe_wraps_groq_failure() -> None:
    fake = _FakeGroq()
    fake.audio.transcriptions.error = RuntimeError("groq exploded")
    with pytest.raises(TranscriptionError) as exc:
        await transcribe_voice(b"x", "audio/ogg", client=fake)  # type: ignore[arg-type]
    assert "groq exploded" in str(exc.value)


@pytest.mark.asyncio
async def test_transcribe_tolerates_string_result() -> None:
    """If Groq ever returns a bare string, we still extract text."""

    class StrTranscriptions:
        def __init__(self) -> None:
            self.calls: list[dict] = []

        def create(self, **kwargs):
            self.calls.append(kwargs)
            return "bare string transcript"

    class StrAudio:
        transcriptions = StrTranscriptions()

    class StrClient:
        audio = StrAudio()

    result = await transcribe_voice(b"x", "audio/ogg", client=StrClient())  # type: ignore[arg-type]
    assert result == "bare string transcript"
