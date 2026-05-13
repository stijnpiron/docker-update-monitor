"""Database schema migrations for the state DB."""

import sqlite3


def _unique_index_has_new_version(conn: sqlite3.Connection) -> bool:
    """Return True if the updates table still has its UNIQUE index on new_version."""
    for row in conn.execute("PRAGMA index_list(updates)").fetchall():
        if not row[2]:  # not a unique index
            continue
        cols = {r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})").fetchall()}
        if "new_version" in cols:
            return True
    return False


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

    # Change UNIQUE constraint from (container_name, image, new_version, update_type) to
    # (container_name, image, current_version, update_type) so that repeated digest changes
    # for the same rolling tag replace the single row rather than accumulating entries.
    if _unique_index_has_new_version(conn):
        conn.execute("""\
            CREATE TABLE updates_new (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                container_name TEXT NOT NULL,
                service_name TEXT NOT NULL DEFAULT '',
                image TEXT NOT NULL,
                current_version TEXT NOT NULL,
                new_version TEXT NOT NULL,
                update_type TEXT NOT NULL,
                stack TEXT NOT NULL DEFAULT '',
                first_seen_at TEXT NOT NULL,
                last_seen_at TEXT NOT NULL,
                notified_at TEXT,
                resolved_at TEXT,
                UNIQUE(container_name, image, current_version, update_type)
            )
        """)
        # Keep the most recently seen row per new unique key to discard stale duplicates.
        conn.execute("""\
            INSERT INTO updates_new (id, container_name, service_name, image, current_version,
                                     new_version, update_type, stack, first_seen_at, last_seen_at,
                                     notified_at, resolved_at)
            SELECT id, container_name, service_name, image, current_version,
                   new_version, update_type, stack, first_seen_at, last_seen_at,
                   notified_at, resolved_at
            FROM updates
            WHERE id IN (
                SELECT MAX(id)
                FROM updates
                GROUP BY container_name, image, current_version, update_type
            )
        """)
        conn.execute("DROP TABLE updates")
        conn.execute("ALTER TABLE updates_new RENAME TO updates")
