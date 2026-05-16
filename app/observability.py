"""Langfuse LLM observability — optional, gated on env vars.

When both ``LANGFUSE_PUBLIC_KEY`` and ``LANGFUSE_SECRET_KEY`` are set,
``@observe()`` decorated functions emit spans, and you'll see a trace
per user interaction in the Langfuse dashboard. The orchestrator's
``handle_incoming_message`` / ``handle_callback`` are the trace roots,
and downstream LLM / DB / Splitwise calls become child spans.

When the keys are absent, the decorator is a no-op and nothing
leaves the host — useful for tests, dev iteration without an account,
or temporarily disabling observability without code changes.

Helper functions:
    * :func:`observe` — decorator. Use ``@observe()`` or
      ``@observe(name="...", as_type="generation", capture_input=False)``.
    * :func:`update_span` — set ``input`` / ``output`` / ``metadata`` on
      the currently-active span (useful when you want to override what
      the decorator auto-captured, e.g. to exclude raw image bytes).
    * :func:`trace_context` — context manager that stamps trace-level
      attributes (``tags``, ``user_id``, ``session_id``, ``metadata``)
      onto the current span AND propagates them to every child span
      created inside the ``with`` block. Used at the top of root
      handlers to tag traces by environment / user / chat session.
    * :func:`is_enabled` — boolean: was Langfuse successfully initialized.
"""

from __future__ import annotations

import logging
import os
from contextlib import nullcontext
from typing import Any

from app.config import settings

log = logging.getLogger(__name__)


def _enabled() -> bool:
    return bool(
        settings.langfuse_public_key.get_secret_value()
        and settings.langfuse_secret_key.get_secret_value()
    )


_LANGFUSE_ENABLED = _enabled()

# Lazily import Langfuse only when enabled — keeps cold-start cheap and
# means a missing dep won't crash the bot in disabled mode.
_langfuse_client: Any | None = None
_langfuse_observe: Any | None = None
_langfuse_propagate: Any | None = None

if _LANGFUSE_ENABLED:
    try:
        # Langfuse SDK reads from env vars; mirror our settings there
        # so the SDK doesn't need to know about pydantic-settings.
        os.environ.setdefault(
            "LANGFUSE_PUBLIC_KEY", settings.langfuse_public_key.get_secret_value()
        )
        os.environ.setdefault(
            "LANGFUSE_SECRET_KEY", settings.langfuse_secret_key.get_secret_value()
        )
        if settings.langfuse_host:
            os.environ.setdefault("LANGFUSE_HOST", settings.langfuse_host)

        from langfuse import (  # noqa: E402
            get_client,
            observe as _langfuse_observe,
            propagate_attributes as _langfuse_propagate,
        )

        _langfuse_client = get_client()
        log.info("langfuse observability enabled host=%s", settings.langfuse_host)
    except Exception as exc:  # noqa: BLE001 — never break the bot on observability errors
        log.warning("langfuse init failed; observability disabled: %s", exc)
        _LANGFUSE_ENABLED = False
        _langfuse_client = None
        _langfuse_observe = None
        _langfuse_propagate = None
else:
    log.info("langfuse observability disabled (keys not set)")


def is_enabled() -> bool:
    return _LANGFUSE_ENABLED


def observe(
    func: Any = None,
    *,
    name: str | None = None,
    as_type: str | None = None,
    capture_input: bool | None = None,
    capture_output: bool | None = None,
) -> Any:
    """Wrap a function with a Langfuse span.

    Identical signature to ``langfuse.observe`` (a subset of the kwargs).
    Falls back to a no-op when Langfuse is disabled so the bot keeps
    running without keys.
    """
    if not _LANGFUSE_ENABLED or _langfuse_observe is None:
        # Bare @observe (no parens): first arg is the function.
        if func is not None and callable(func):
            return func

        # Parameterized: return a passthrough decorator.
        def _passthrough(fn: Any) -> Any:
            return fn

        return _passthrough

    # Bare @observe — Langfuse handles this shape directly.
    if func is not None:
        return _langfuse_observe(func)

    return _langfuse_observe(
        name=name,
        as_type=as_type,  # type: ignore[arg-type]
        capture_input=capture_input,
        capture_output=capture_output,
    )


def update_span(
    *,
    input: Any | None = None,  # noqa: A002 — match Langfuse's param name
    output: Any | None = None,
    metadata: Any | None = None,
    name: str | None = None,
) -> None:
    """Override what got captured on the current span. No-op when disabled."""
    if not _LANGFUSE_ENABLED or _langfuse_client is None:
        return
    try:
        _langfuse_client.update_current_span(
            input=input, output=output, metadata=metadata, name=name
        )
    except Exception:  # noqa: BLE001
        log.exception("langfuse update_span failed")


def trace_context(
    *,
    user_id: str | None = None,
    session_id: str | None = None,
    tags: list[str] | None = None,
    metadata: dict[str, str] | None = None,
) -> Any:
    """Context manager that propagates trace attributes to all child spans.

    Use at the top of root-span handlers to apply environment tags,
    user identification, session grouping, etc. Attributes set here
    show up on the trace AND on every child span created inside the
    ``with`` block.

    Automatically adds the configured ``settings.environment`` value to
    ``tags`` (so e.g. dev runs are filterable from prod runs in the
    Langfuse dashboard).

    No-op when Langfuse is disabled — the ``with`` block just runs normally.

    Example:
        async def handle_incoming_message(update):
            ...
            with trace_context(
                user_id=str(telegram_user_id),
                session_id=f"chat-{chat_id}",
                tags=["photo"] if has_photo else None,
            ):
                # all child spans (transcribe, parse, ...) get tagged.
                ...
    """
    if not _LANGFUSE_ENABLED or _langfuse_propagate is None:
        return nullcontext()

    # Auto-tag every trace with the environment label.
    env_tag = settings.environment.strip()
    merged_tags: list[str] | None
    if env_tag:
        merged_tags = [env_tag, *(tags or [])]
    else:
        merged_tags = tags

    # Langfuse rejects non-string metadata values; coerce defensively.
    stringified_metadata: dict[str, str] | None = (
        {k: str(v) for k, v in metadata.items()} if metadata else None
    )

    return _langfuse_propagate(
        user_id=user_id,
        session_id=session_id,
        tags=merged_tags,
        metadata=stringified_metadata,
    )


__all__ = ["is_enabled", "observe", "trace_context", "update_span"]
