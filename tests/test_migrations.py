"""Tests for database migrations."""

import sqlite3

from app.migrations import run_migrations


_OLD_SCHEMA = """\
CREATE TABLE updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    image TEXT NOT NULL,
    current_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    update_type TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT,
    resolved_at TEXT,
    UNIQUE(container_name, image, new_version, update_type)
);
"""


class TestMigrations:
    def _make_old_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(_OLD_SCHEMA)
        conn.commit()
        return conn

    def test_adds_service_name_column(self):
        conn = self._make_old_db()
        run_migrations(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}
        assert "service_name" in cols
        conn.close()

    def test_adds_stack_column(self):
        conn = self._make_old_db()
        run_migrations(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}
        assert "stack" in cols
        conn.close()

    def test_idempotent_on_current_schema(self):
        """Running migrations on a DB that already has all migrations applied does nothing."""
        conn = sqlite3.connect(":memory:")
        conn.execute(_OLD_SCHEMA)
        conn.execute("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")
        conn.commit()
        # First run applies the constraint migration
        run_migrations(conn)
        # Second run is a no-op
        run_migrations(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}
        assert "service_name" in cols
        assert "stack" in cols
        conn.close()

    def test_non_unique_index_does_not_trigger_rebuild(self):
        """A non-unique index covering new_version must not be mistaken for the
        legacy UNIQUE(new_version) constraint, so no table rebuild is triggered."""
        conn = self._make_old_db()
        conn.execute("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")
        conn.commit()
        # Apply the constraint migration — UNIQUE now on current_version, not new_version.
        run_migrations(conn)
        # A plain (non-unique) index that happens to cover new_version.
        conn.execute("CREATE INDEX idx_new_version ON updates(new_version)")
        conn.commit()

        # Second pass must skip the non-unique index and leave the table untouched.
        run_migrations(conn)

        index_names = {row[1] for row in conn.execute("PRAGMA index_list(updates)").fetchall()}
        assert "idx_new_version" in index_names  # survives → table was not rebuilt
        conn.close()

    def test_migrates_unique_constraint_to_current_version(self):
        """Migration replaces UNIQUE(new_version) with UNIQUE(current_version)."""
        conn = self._make_old_db()
        run_migrations(conn)

        unique_cols: set[str] = set()
        for idx_row in conn.execute("PRAGMA index_list(updates)").fetchall():
            if not idx_row[2]:
                continue
            for col_row in conn.execute(f"PRAGMA index_info({idx_row[1]})").fetchall():
                unique_cols.add(col_row[2])

        assert "current_version" in unique_cols
        assert "new_version" not in unique_cols
        conn.close()

    def test_migration_deduplicates_digest_rows(self):
        """Migration keeps only the most recent row when two rows share the same
        (container_name, image, current_version, update_type) after the constraint change."""
        conn = self._make_old_db()
        # Add service_name and stack before inserting data (as prior migrations would)
        conn.execute("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")
        conn.execute(
            """INSERT INTO updates (container_name, service_name, image, current_version,
                                    new_version, update_type, stack, first_seen_at, last_seen_at)
               VALUES ('app', 'app', 'myimage', 'dev', 'sha-aaaaaaa', 'digest', '', 't1', 't1')"""
        )
        conn.execute(
            """INSERT INTO updates (container_name, service_name, image, current_version,
                                    new_version, update_type, stack, first_seen_at, last_seen_at)
               VALUES ('app', 'app', 'myimage', 'dev', 'sha-bbbbbbb', 'digest', '', 't2', 't2')"""
        )
        conn.commit()

        run_migrations(conn)

        rows = conn.execute("SELECT new_version FROM updates").fetchall()
        assert len(rows) == 1
        assert rows[0][0] == "sha-bbbbbbb"
        conn.close()

    def test_insert_works_after_migration(self):
        conn = self._make_old_db()
        run_migrations(conn)
        conn.execute(
            """INSERT INTO updates
               (container_name, service_name, image, current_version, new_version,
                update_type, stack, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            ("ctr", "svc", "img:1", "1.0", "2.0", "major", "mystack", "t1", "t1"),
        )
        conn.commit()
        row = conn.execute("SELECT service_name, stack FROM updates").fetchone()
        assert row == ("svc", "mystack")
        conn.close()


def _unique_index_cols(conn: sqlite3.Connection) -> set[str]:
    for idx_row in conn.execute("PRAGMA index_list(updates)").fetchall():
        if not idx_row[2]:
            continue
        return {r[2] for r in conn.execute(f"PRAGMA index_info({idx_row[1]})").fetchall()}
    return set()


# An intermediate (post-task-02) schema: UNIQUE on current_version but no host
# column yet.  This is the real-world DB that task 03's migration must handle.
_OLD_HOSTLESS_SCHEMA = """\
CREATE TABLE updates (
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
);
"""


class TestHostMigration:
    def _make_hostless_db(self) -> sqlite3.Connection:
        conn = sqlite3.connect(":memory:")
        conn.execute(_OLD_HOSTLESS_SCHEMA)
        conn.commit()
        return conn

    def test_migration_adds_host_column_with_local_default(self):
        """AC2: pre-feature rows are preserved and back-filled with host='local'."""
        conn = self._make_hostless_db()
        conn.execute(
            """INSERT INTO updates (container_name, service_name, image, current_version,
                                    new_version, update_type, stack, first_seen_at, last_seen_at)
               VALUES ('web', 'web', 'nginx', '1.0.0', '1.1.0', 'minor', 'mystack', 't1', 't1')"""
        )
        conn.commit()

        run_migrations(conn)

        cols = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}
        assert "host" in cols
        row = conn.execute(
            "SELECT container_name, new_version, host FROM updates"
        ).fetchone()
        assert row == ("web", "1.1.0", "local")
        conn.close()

    def test_migration_unique_key_includes_host(self):
        conn = self._make_hostless_db()
        run_migrations(conn)
        unique = _unique_index_cols(conn)
        assert unique == {"host", "container_name", "image", "current_version", "update_type"}
        conn.close()

    def test_migration_is_idempotent(self):
        """AC3: running migrations twice is a no-op the second time."""
        conn = self._make_hostless_db()
        conn.execute(
            """INSERT INTO updates (container_name, service_name, image, current_version,
                                    new_version, update_type, stack, first_seen_at, last_seen_at)
               VALUES ('web', 'web', 'nginx', '1.0.0', '1.1.0', 'minor', 'mystack', 't1', 't1')"""
        )
        conn.commit()

        run_migrations(conn)
        first = conn.execute("SELECT id, container_name, host FROM updates").fetchall()

        run_migrations(conn)
        second = conn.execute("SELECT id, container_name, host FROM updates").fetchall()

        assert first == second
        assert len(first) == 1

        # A plain non-unique index on a single column survives both passes
        # (proving no spurious rebuild happened on the second run).
        conn.execute("CREATE INDEX idx_custom ON updates(image)")
        run_migrations(conn)
        index_names = {row[1] for row in conn.execute("PRAGMA index_list(updates)").fetchall()}
        assert "idx_custom" in index_names
        conn.close()

    def test_fresh_schema_unique_key_includes_host(self):
        """AC1: a fresh DB (via the _SCHEMA path) already has a host-scoped key."""
        conn = self._conn_with_fresh_schema()
        unique = _unique_index_cols(conn)
        assert unique == {"host", "container_name", "image", "current_version", "update_type"}
        conn.close()

    def test_fresh_schema_has_host_status_and_event_cooldowns(self):
        """AC1: both new tables are created on a fresh database."""
        conn = self._conn_with_fresh_schema()
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "host_status" in names
        assert "event_cooldowns" in names
        conn.close()

    def test_migration_creates_host_status_and_event_cooldowns(self):
        """Pre-feature DB gains host_status and event_cooldowns during migration."""
        conn = self._make_hostless_db()
        run_migrations(conn)
        names = {row[0] for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'"
        ).fetchall()}
        assert "host_status" in names
        assert "event_cooldowns" in names
        conn.close()

    @staticmethod
    def _conn_with_fresh_schema() -> sqlite3.Connection:
        import app.state as state
        conn = sqlite3.connect(":memory:")
        conn.row_factory = sqlite3.Row
        conn.execute(state._SCHEMA)
        conn.execute(state._DIGESTS_SCHEMA)
        conn.execute(state._METADATA_SCHEMA)
        conn.execute(state._HOST_STATUS_SCHEMA)
        conn.execute(state._EVENT_COOLDOWNS_SCHEMA)
        return conn
