"""Tiny, dependency-free migration runner.

Applies numbered `.sql` files from `migrations/` in order, tracking what has
already run in a `schema_migrations` table. Each migration runs in its own
transaction. No down-migrations by design: fix forward.

Usage:
    python -m stockbot.migrate                 # uses DATABASE_URL
    python -m stockbot.migrate --database-url postgresql://...
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import psycopg

MIGRATIONS_DIR = Path(__file__).resolve().parent.parent.parent / "migrations"


def _migration_files() -> list[Path]:
    return sorted(MIGRATIONS_DIR.glob("*.sql"), key=lambda p: p.name)


def run_migrations(database_url: str) -> list[str]:
    """Apply all pending migrations. Returns the names that were applied."""
    applied: list[str] = []
    with psycopg.connect(database_url, autocommit=False) as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    name TEXT PRIMARY KEY,
                    applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
                )
                """
            )
        conn.commit()

        with conn.cursor() as cur:
            cur.execute("SELECT name FROM schema_migrations")
            already_applied = {row[0] for row in cur.fetchall()}

        for path in _migration_files():
            if path.name in already_applied:
                continue
            sql = path.read_text(encoding="utf-8")
            with conn.cursor() as cur:
                cur.execute(sql)
                cur.execute("INSERT INTO schema_migrations (name) VALUES (%s)", (path.name,))
            conn.commit()
            applied.append(path.name)
    return applied


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=None,
        help="Postgres connection string. Defaults to DATABASE_URL / .env settings.",
    )
    args = parser.parse_args(argv)

    database_url = args.database_url
    if database_url is None:
        from stockbot.config import get_settings

        database_url = get_settings().database_url

    applied = run_migrations(database_url)
    if applied:
        print(f"Applied {len(applied)} migration(s): {', '.join(applied)}")
    else:
        print("No pending migrations.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
