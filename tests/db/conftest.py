"""Shared fixtures for Brick E (persistence) tests.

We never hit a real Supabase project — instead we install a
``FakeSupabaseClient`` in place of the cached client returned by
``app.db.client.get_supabase``.

The fake records every call as a ``Call`` tuple so tests can assert on
the exact query chain (table name, operation, args, filters, execute
result). It is intentionally dumb: it returns whatever canned response
the test queued up, in FIFO order.
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest


@dataclass
class Call:
    """A single recorded call in a query chain."""

    method: str
    args: tuple[Any, ...] = ()
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class FakeResponse:
    """Mimics the ``APIResponse`` shape used by ``supabase-py`` (just ``.data``)."""

    data: list[dict[str, Any]] | None


class FakeQuery:
    """Records the chain; returns ``self`` for every builder method; on
    ``execute()`` pops the next canned response from the client's queue.
    """

    # Builder methods that just record themselves and return self.
    _CHAIN_METHODS = {
        "select",
        "insert",
        "upsert",
        "update",
        "delete",
        "eq",
        "lt",
        "gt",
        "lte",
        "gte",
        "neq",
        "in_",
        "limit",
        "order",
        "range",
    }

    def __init__(self, client: FakeSupabaseClient, table_name: str) -> None:
        self.client = client
        self.table_name = table_name
        self.chain: list[Call] = []

    def __getattr__(self, name: str) -> Any:
        if name in self._CHAIN_METHODS:

            def _record(*args: Any, **kwargs: Any) -> FakeQuery:
                self.chain.append(Call(name, args, kwargs))
                return self

            return _record
        raise AttributeError(name)

    def execute(self) -> FakeResponse:
        # Snapshot for assertions, including the table name as the first
        # entry so tests can read it off uniformly.
        self.client.calls.append((self.table_name, self.chain))
        if not self.client.responses:
            return FakeResponse(data=[])
        return self.client.responses.pop(0)


class FakeSupabaseClient:
    """Drop-in replacement for ``supabase.Client`` in unit tests."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[Call]]] = []
        self.responses: list[FakeResponse] = []

    def queue(self, data: list[dict[str, Any]] | None) -> None:
        """Queue the next ``execute()`` response."""
        self.responses.append(FakeResponse(data=data))

    def table(self, name: str) -> FakeQuery:
        return FakeQuery(self, name)


@pytest.fixture
def fake_supabase(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeSupabaseClient]:
    """Patch ``get_supabase`` everywhere to return a fresh fake."""
    fake = FakeSupabaseClient()

    # Patch the source ``get_supabase`` plus every module that imported it
    # by name (``from app.db.client import get_supabase``).
    monkeypatch.setattr("app.db.client.get_supabase", lambda: fake)
    monkeypatch.setattr("app.db.users.get_supabase", lambda: fake)
    monkeypatch.setattr("app.db.groups.get_supabase", lambda: fake)
    monkeypatch.setattr("app.db.expenses.get_supabase", lambda: fake)
    yield fake
