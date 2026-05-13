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
