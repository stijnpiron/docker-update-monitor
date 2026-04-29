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
        """Running migrations on a DB that already has the columns does nothing."""
        conn = sqlite3.connect(":memory:")
        conn.execute(_OLD_SCHEMA)
        conn.execute("ALTER TABLE updates ADD COLUMN service_name TEXT NOT NULL DEFAULT ''")
        conn.execute("ALTER TABLE updates ADD COLUMN stack TEXT NOT NULL DEFAULT ''")
        conn.commit()
        # Should not raise
        run_migrations(conn)
        cols = {row[1] for row in conn.execute("PRAGMA table_info(updates)").fetchall()}
        assert "service_name" in cols
        assert "stack" in cols
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
