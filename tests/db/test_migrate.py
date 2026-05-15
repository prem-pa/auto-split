"""Tests for ``scripts/migrate.py``.

Mocks ``psycopg.connect`` so no live DB is required. Verifies:
    * The bootstrap CREATE TABLE statement is always issued.
    * Only pending migrations are executed.
    * Re-running with all migrations already applied is a no-op.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from pydantic import SecretStr


def _load_migrate_module() -> Any:
    """Import ``scripts/migrate.py`` as a module without polluting sys.modules."""
    project_root = Path(__file__).resolve().parents[2]
    script_path = project_root / "scripts" / "migrate.py"
    spec = importlib.util.spec_from_file_location("migrate_under_test", script_path)
    assert spec is not None and spec.loader is not None
    mod = importlib.util.module_from_spec(spec)
    # Ensure ``app`` is importable inside the script.
    if str(project_root) not in sys.path:
        sys.path.insert(0, str(project_root))
    spec.loader.exec_module(mod)
    return mod


class _FakeCursor:
    """Records every ``execute`` call; supports the cursor context manager."""

    def __init__(self, fetchall_results: list[list[tuple[Any, ...]]]) -> None:
        self.executed: list[tuple[str, tuple[Any, ...] | None]] = []
        self._fetchall_results = list(fetchall_results)

    def __enter__(self) -> _FakeCursor:
        return self

    def __exit__(self, *exc: Any) -> None:
        return None

    def execute(self, sql: str, params: tuple[Any, ...] | None = None) -> None:
        self.executed.append((sql, params))

    def fetchall(self) -> list[tuple[Any, ...]]:
        if not self._fetchall_results:
            return []
        return self._fetchall_results.pop(0)


class _FakeConn:
    def __init__(self, cursor: _FakeCursor) -> None:
        self._cursor = cursor
        self.commits = 0
        self.closed = False

    def cursor(self) -> _FakeCursor:
        return self._cursor

    def commit(self) -> None:
        self.commits += 1

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def _set_db_url(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "supabase_db_url", SecretStr("postgres://fake/db"))


def test_missing_db_url_exits_with_code_2(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.config import settings

    monkeypatch.setattr(settings, "supabase_db_url", SecretStr(""))
    mod = _load_migrate_module()
    with pytest.raises(SystemExit) as exc:
        mod.run()
    assert exc.value.code == 2


def test_applies_pending_migrations(
    monkeypatch: pytest.MonkeyPatch,
    _set_db_url: None,
) -> None:
    mod = _load_migrate_module()

    # schema_migrations is empty → every discovered migration is pending.
    cursor = _FakeCursor(fetchall_results=[[]])
    conn = _FakeConn(cursor)
    connect = MagicMock(return_value=conn)
    monkeypatch.setattr(mod.psycopg, "connect", connect)

    rc = mod.run()
    assert rc == 0

    sqls = [s for s, _ in cursor.executed]
    # Bootstrap always runs first.
    assert "CREATE TABLE IF NOT EXISTS schema_migrations" in sqls[0]
    # SELECT to fetch applied migrations.
    assert any("SELECT name FROM schema_migrations" in s for s in sqls)
    # The init migration's CREATE TABLE statements should appear.
    assert any("CREATE TABLE IF NOT EXISTS users" in s for s in sqls)
    # And the migration filename gets recorded.
    insert_calls = [
        params for s, params in cursor.executed if "INSERT INTO schema_migrations" in s
    ]
    assert insert_calls
    recorded_names = [p[0] for p in insert_calls if p]
    assert "0001_init.sql" in recorded_names

    # Connection is always closed.
    assert conn.closed


def test_idempotent_when_already_applied(
    monkeypatch: pytest.MonkeyPatch,
    _set_db_url: None,
) -> None:
    mod = _load_migrate_module()
    # Pretend the init migration is already applied.
    cursor = _FakeCursor(fetchall_results=[[("0001_init.sql",)]])
    conn = _FakeConn(cursor)
    monkeypatch.setattr(mod.psycopg, "connect", MagicMock(return_value=conn))

    rc = mod.run()
    assert rc == 0

    sqls = [s for s, _ in cursor.executed]
    # The CREATE TABLE for ``users`` should NOT have been re-applied.
    assert not any("CREATE TABLE IF NOT EXISTS users" in s for s in sqls)
    # And no INSERT into schema_migrations should have happened.
    assert not any("INSERT INTO schema_migrations" in s for s in sqls)


def test_dry_run_does_not_execute_migration_sql(
    monkeypatch: pytest.MonkeyPatch,
    _set_db_url: None,
) -> None:
    mod = _load_migrate_module()
    cursor = _FakeCursor(fetchall_results=[[]])
    conn = _FakeConn(cursor)
    monkeypatch.setattr(mod.psycopg, "connect", MagicMock(return_value=conn))

    rc = mod.run(dry_run=True)
    assert rc == 0
    sqls = [s for s, _ in cursor.executed]
    # Bootstrap still runs, but no domain CREATE TABLE statements.
    assert any("CREATE TABLE IF NOT EXISTS schema_migrations" in s for s in sqls)
    assert not any("CREATE TABLE IF NOT EXISTS users" in s for s in sqls)


def test_list_only_does_not_execute_migrations(
    monkeypatch: pytest.MonkeyPatch,
    _set_db_url: None,
) -> None:
    mod = _load_migrate_module()
    cursor = _FakeCursor(fetchall_results=[[]])
    conn = _FakeConn(cursor)
    monkeypatch.setattr(mod.psycopg, "connect", MagicMock(return_value=conn))

    rc = mod.run(list_only=True)
    assert rc == 0
    sqls = [s for s, _ in cursor.executed]
    assert not any("CREATE TABLE IF NOT EXISTS users" in s for s in sqls)
