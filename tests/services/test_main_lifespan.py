"""Verify the FastAPI ``lifespan`` registers + runs the TTL sweep task."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from fastapi.testclient import TestClient


@pytest.mark.asyncio
async def test_lifespan_spawns_sweep_and_cancels_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []

    async def fake_sweep() -> int:
        calls.append(None)
        return 0

    # Patch where ``_sweep_loop`` resolves it.
    monkeypatch.setattr("app.main.sweep_expired_pending", fake_sweep)

    from app.main import create_app

    # Use a tiny interval so the loop ticks at least once during the
    # short ``with TestClient`` window.
    app = create_app(sweep_interval_seconds=0.05)

    with TestClient(app):
        # Give the background task time to tick. The interval is 50ms,
        # so 300ms is comfortably enough for 2-3 ticks.
        await asyncio.sleep(0.3)

    # Loop ran at least once.
    assert len(calls) >= 1


@pytest.mark.asyncio
async def test_lifespan_swallows_sweep_errors_and_keeps_ticking(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[None] = []

    async def flaky_sweep() -> int:
        calls.append(None)
        if len(calls) == 1:
            raise RuntimeError("transient supabase blip")
        return 3

    monkeypatch.setattr("app.main.sweep_expired_pending", flaky_sweep)

    from app.main import create_app

    app = create_app(sweep_interval_seconds=0.05)
    with TestClient(app):
        await asyncio.sleep(0.3)

    # Despite the first call raising, the loop tried again.
    assert len(calls) >= 2


def test_health_endpoint_still_present() -> None:
    """Cross-check that wiring the lifespan didn't break the existing route."""
    from app.main import create_app

    app = create_app(sweep_interval_seconds=3600.0)
    paths = sorted(r.path for r in app.routes)  # type: ignore[attr-defined]
    assert "/health" in paths
    assert "/telegram/webhook" in paths
    assert "/oauth/callback" in paths


def test_create_app_default_interval_is_60_seconds(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no override is passed, we use the module-level default."""

    captured: dict[str, Any] = {}

    async def fake_loop(interval: float) -> None:
        captured["interval"] = interval
        await asyncio.sleep(60)  # never actually returns within the test

    monkeypatch.setattr("app.main._sweep_loop", fake_loop)

    from app.main import SWEEP_INTERVAL_SECONDS, create_app

    app = create_app()
    with TestClient(app):
        # tiny window so the task at least starts
        pass

    assert captured.get("interval") == SWEEP_INTERVAL_SECONDS
