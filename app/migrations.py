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


def rename_local_host_rows(conn: sqlite3.Connection, new_local_name: str) -> None:
    """One-shot rename of the legacy ``'local'`` host label to *new_local_name*.

    Called from ``state._connect`` after schema migrations, driven by
    ``LOCAL_HOST_NAME``. When the operator first renames the local daemon,
    existing rows still labelled ``'local'`` are renamed so history keeps
    belonging to the same daemon — the dashboard strip, webhook payloads,
    and host-status down/recovered continuity all follow the new name.

    Runs at most once per database: completion is recorded in the
    ``metadata`` table under ``local_host_renamed_to``. The marker is set
    even when there are no ``'local'`` rows to rename, so a *remote* host
    literally named ``local`` (legal once the local daemon has a different
    name) is never mistaken for legacy local data on a later restart.

    Assumes all tables already exist — true for the one caller,
    ``state._connect``, which runs this after schema migrations.
    """
    if new_local_name == "local":
        return

    already = conn.execute(
        "SELECT value FROM metadata WHERE key = 'local_host_renamed_to'"
    ).fetchone()
    if already is not None and already[0]:
        return

    has_local = conn.execute(
        "SELECT 1 FROM updates WHERE host = 'local' LIMIT 1"
    ).fetchone()
    has_new = conn.execute(
        "SELECT 1 FROM updates WHERE host = ? LIMIT 1", (new_local_name,)
    ).fetchone()
    if has_local is not None and has_new is None:
        # Skip when a row for the new name exists: the UNIQUE constraint
        # would reject the UPDATE, and its presence means this data was
        # already written under the new name.
        conn.execute(
            "UPDATE updates SET host = ? WHERE host = 'local'",
            (new_local_name,),
        )
    has_local = conn.execute(
        "SELECT 1 FROM host_status WHERE host = 'local' LIMIT 1"
    ).fetchone()
    has_new = conn.execute(
        "SELECT 1 FROM host_status WHERE host = ? LIMIT 1", (new_local_name,)
    ).fetchone()
    if has_local is not None and has_new is None:
        # host is the PRIMARY KEY — a pre-existing row for the new name
        # would also make a plain UPDATE raise IntegrityError.
        conn.execute(
            "UPDATE host_status SET host = ? WHERE host = 'local'",
            (new_local_name,),
        )
    # Cooldown keys are namespaced "host:<name>"; host names cannot contain
    # ':' so the exact key match is unambiguous.
    conn.execute(
        "UPDATE event_cooldowns SET key = ? WHERE key = 'host:local'",
        (f"host:{new_local_name}",),
    )

    conn.execute(
        "INSERT INTO metadata (key, value) VALUES ('local_host_renamed_to', ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (new_local_name,),
    )
    conn.commit()


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
                checked_at TEXT,
                down_since TEXT
            )
        """)
    else:
        # Downgrade/upgrade path: older databases have host_status without the
        # down_since column (the "unreachable since" transition-time, D2).  Pre-
        # migration down rows keep down_since NULL — the exact transition time
        # is not recoverable; it is populated on the next real transition.
        hs_cols = {row[1] for row in conn.execute("PRAGMA table_info(host_status)").fetchall()}
        if "down_since" not in hs_cols:
            conn.execute("ALTER TABLE host_status ADD COLUMN down_since TEXT")
    if "event_cooldowns" not in tables:
        conn.execute("""\
            CREATE TABLE IF NOT EXISTS event_cooldowns (
                key TEXT PRIMARY KEY,
                last_fired_at TEXT
            )
        """)
