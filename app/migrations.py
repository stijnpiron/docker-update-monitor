"""Database schema migrations for the state DB."""

import sqlite3


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations to an existing database."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}

    if "service_name" not in existing:
        conn.execute("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")

    if "stack" not in existing:
        conn.execute("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")
