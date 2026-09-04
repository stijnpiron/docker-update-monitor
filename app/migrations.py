"""Database schema migrations for the state DB."""

import sqlite3


def _unique_index_cols(conn: sqlite3.Connection) -> set[str]:
    """Return the column set of the first UNIQUE index on the updates table."""
    for row in conn.execute("PRAGMA index_list(updates)").fetchall():
        if not row[2]:  # not a unique index
            continue
        return {r[2] for r in conn.execute(f"PRAGMA index_info({row[1]})").fetchall()}
    return set()


def _rebuild_updates_with_host(conn: sqlite3.Connection) -> None:
    """Rebuild the updates table so the UNIQUE key includes host.

    All rows in a pre-host database belong to the local daemon, so they are
    back-filled with ``'local'``.  For each new unique key
    ``(host, container_name, image, current_version, update_type)`` only the
    most recently seen row is retained, discarding stale duplicates.
    """
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
            host TEXT NOT NULL DEFAULT 'local',
            UNIQUE(host, container_name, image, current_version, update_type)
        )
    """)
    conn.execute("""\
        INSERT INTO updates_new (id, container_name, service_name, image, current_version,
                                 new_version, update_type, stack, first_seen_at, last_seen_at,
                                 notified_at, resolved_at, host)
        SELECT id, container_name, service_name, image, current_version,
               new_version, update_type, stack, first_seen_at, last_seen_at,
               notified_at, resolved_at, 'local'
        FROM updates
        WHERE id IN (
            SELECT MAX(id)
            FROM updates
            GROUP BY container_name, image, current_version, update_type
        )
    """)
    conn.execute("DROP TABLE updates")
    conn.execute("ALTER TABLE updates_new RENAME TO updates")


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

    # Rebuild the updates table if the UNIQUE constraint does not yet include
    # the host column.  This covers every pre-host schema (both the very old
    # UNIQUE(new_version) and the intermediate UNIQUE(current_version)) in one
    # pass.
    if "host" not in _unique_index_cols(conn):
        _rebuild_updates_with_host(conn)

    # Ensure host_status and event_cooldowns exist (for databases created before
    # the multi-host feature).
    tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
    if "host_status" not in tables:
        conn.execute("""\
            CREATE TABLE IF NOT EXISTS host_status (
                host TEXT PRIMARY KEY,
                reachable INTEGER NOT NULL,
                error TEXT,
                checked_at TEXT
            )
        """)
    if "event_cooldowns" not in tables:
        conn.execute("""\
            CREATE TABLE IF NOT EXISTS event_cooldowns (
                key TEXT PRIMARY KEY,
                last_fired_at TEXT
            )
        """)
