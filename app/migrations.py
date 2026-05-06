"""Database schema migrations for the state DB."""

import sqlite3


def run_migrations(conn: sqlite3.Connection) -> None:
    """Apply all pending migrations to an existing database."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}

    if "service_name" not in existing:
        conn.execute("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")

    if "stack" not in existing:
        conn.execute("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")

    # Ensure the digests table exists (for databases created before this feature)
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "digests" not in tables:
        conn.execute("""\
            CREATE TABLE IF NOT EXISTS digests (
                image TEXT NOT NULL,
                tag TEXT NOT NULL,
                digest TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                PRIMARY KEY (image, tag)
            )
        """)
