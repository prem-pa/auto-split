"""Apply SQL migrations to the configured Supabase Postgres database.

Usage:
    uv run python scripts/migrate.py            # apply pending
    uv run python scripts/migrate.py --list     # show applied + pending
    uv run python scripts/migrate.py --dry-run  # print plan, don't execute

Behaviour:
    * Reads ``.sql`` files from ``app/db/migrations/`` in lexical order.
    * Each file is wrapped in a transaction. The filename is recorded in
      ``schema_migrations`` on success; if the file is already in that
      table, it is skipped — so re-running is a no-op.
    * Uses the direct Postgres connection string from
      ``settings.supabase_db_url``. The PostgREST/supabase-py SDK has no
      generic raw-SQL endpoint; the Postgres URI is the documented path
      for migrations (see Supabase dashboard → Project Settings →
      Database → Connection string).

This is intentionally naive — alembic would be overkill at this scale.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

# Make the project root importable when invoked via ``uv run python``.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import psycopg  # noqa: E402 — must follow sys.path tweak

from app.config import settings  # noqa: E402

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent / "app" / "db" / "migrations"

# Bootstrap statement: the very first migration creates schema_migrations,
# but we still need *some* table to record into before that runs. We run
# this CREATE first, then check schema_migrations to decide what to skip.
_BOOTSTRAP_SQL = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    name TEXT PRIMARY KEY,
    applied_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
)
"""


def _discover_migrations() -> list[Path]:
    if not MIGRATIONS_DIR.is_dir():
        raise FileNotFoundError(f"migrations dir not found: {MIGRATIONS_DIR}")
    return sorted(p for p in MIGRATIONS_DIR.glob("*.sql") if p.is_file())


def _connect() -> psycopg.Connection:
    db_url = settings.supabase_db_url.get_secret_value()
    if not db_url:
        sys.stderr.write(
            "missing SUPABASE_DB_URL — copy the URI from the Supabase "
            "dashboard → Project Settings → Database → Connection string.\n"
        )
        raise SystemExit(2)
    # autocommit=False; we manage transactions explicitly per file.
    return psycopg.connect(db_url, autocommit=False)


def _fetch_applied(conn: psycopg.Connection) -> set[str]:
    with conn.cursor() as cur:
        cur.execute("SELECT name FROM schema_migrations")
        return {row[0] for row in cur.fetchall()}


def _apply_one(conn: psycopg.Connection, path: Path) -> None:
    sql = path.read_text(encoding="utf-8")
    with conn.cursor() as cur:
        cur.execute(sql)
        cur.execute(
            "INSERT INTO schema_migrations (name) VALUES (%s) "
            "ON CONFLICT (name) DO NOTHING",
            (path.name,),
        )
    conn.commit()


def run(*, dry_run: bool = False, list_only: bool = False) -> int:
    files = _discover_migrations()
    if not files:
        print(f"no migrations found in {MIGRATIONS_DIR}")
        return 0

    conn = _connect()
    try:
        # Bootstrap the tracking table.
        with conn.cursor() as cur:
            cur.execute(_BOOTSTRAP_SQL)
        conn.commit()
        applied = _fetch_applied(conn)

        pending = [p for p in files if p.name not in applied]

        if list_only:
            print(f"applied ({len(applied)}):")
            for name in sorted(applied):
                print(f"  {name}")
            print(f"pending ({len(pending)}):")
            for p in pending:
                print(f"  {p.name}")
            return 0

        if not pending:
            print("nothing to apply — schema is up to date.")
            return 0

        for p in pending:
            if dry_run:
                print(f"[dry-run] would apply {p.name}")
            else:
                print(f"applying {p.name} ...")
                _apply_one(conn, p)
        return 0
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Apply auto-split SQL migrations to Supabase Postgres."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the migration plan without executing.",
    )
    parser.add_argument(
        "--list",
        dest="list_only",
        action="store_true",
        help="List applied and pending migrations, then exit.",
    )
    args = parser.parse_args()
    return run(dry_run=args.dry_run, list_only=args.list_only)


if __name__ == "__main__":
    raise SystemExit(main())
