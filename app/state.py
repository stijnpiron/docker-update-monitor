import sqlite3
from copy import copy
from datetime import datetime, timezone
from pathlib import Path

import app.config as _config
from app.models import UpdateInfo

_DB_PATH = Path(_config.STATE_DB_PATH)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    image TEXT NOT NULL,
    current_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    update_type TEXT NOT NULL,
    stack TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT,
    resolved_at TEXT,
    UNIQUE(container_name, image, new_version, update_type)
);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    conn.commit()
    return conn


def process_scan(updates: list[UpdateInfo], scan_time: datetime | None = None) -> list[UpdateInfo]:
    """Upsert scan results, resolve absent entries, and return all updates with status set.

    Returns a list of UpdateInfo with ``status`` set to ``"new"``, ``"known"``,
    or ``"resolved"``.
    """
    if scan_time is None:
        scan_time = datetime.now(timezone.utc)

    ts = scan_time.isoformat()
    conn = _connect()
    try:
        # --- upsert current findings ---
        for u in updates:
            conn.execute(
                """INSERT INTO updates (container_name, image, current_version, new_version,
                                        update_type, stack, first_seen_at, last_seen_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(container_name, image, new_version, update_type) DO UPDATE SET
                       last_seen_at = excluded.last_seen_at,
                       current_version = excluded.current_version,
                       stack = excluded.stack,
                       resolved_at = NULL""",
                (u.container_name, u.image, u.current_version, u.new_version,
                 u.update_type, u.stack, ts, ts),
            )

        # --- resolve absent entries ---
        if not updates:
            conn.execute(
                "UPDATE updates SET resolved_at = ? WHERE resolved_at IS NULL",
                (ts,),
            )
        else:
            placeholders = ",".join(["(?, ?, ?, ?)"] * len(updates))
            params: list[str] = []
            for u in updates:
                params.extend([u.container_name, u.image, u.new_version, u.update_type])

            conn.execute(
                f"""UPDATE updates SET resolved_at = ?
                    WHERE resolved_at IS NULL
                    AND (container_name, image, new_version, update_type) NOT IN
                        (VALUES {placeholders})""",
                [ts] + params,
            )

        conn.commit()

        # --- build categorized result ---
        conn.row_factory = sqlite3.Row

        # Newly resolved in this scan
        resolved_rows = conn.execute(
            "SELECT * FROM updates WHERE resolved_at = ? ORDER BY first_seen_at",
            (ts,),
        ).fetchall()

        # Active (non-resolved)
        active_rows = conn.execute(
            "SELECT * FROM updates WHERE resolved_at IS NULL ORDER BY first_seen_at",
        ).fetchall()

        result: list[UpdateInfo] = []

        for row in active_rows:
            status = "new" if row["first_seen_at"] == ts else "known"
            result.append(UpdateInfo(
                container_name=row["container_name"],
                stack=row["stack"],
                image=row["image"],
                current_version=row["current_version"],
                new_version=row["new_version"],
                update_type=row["update_type"],
                status=status,
            ))

        for row in resolved_rows:
            result.append(UpdateInfo(
                container_name=row["container_name"],
                stack=row["stack"],
                image=row["image"],
                current_version=row["current_version"],
                new_version=row["new_version"],
                update_type=row["update_type"],
                status="resolved",
            ))

        return result
    finally:
        conn.close()


def mark_notified(updates: list[UpdateInfo], notified_time: datetime | None = None) -> None:
    """Set notified_at for the given updates."""
    if notified_time is None:
        notified_time = datetime.now(timezone.utc)

    ts = notified_time.isoformat()
    conn = _connect()
    try:
        for u in updates:
            conn.execute(
                """UPDATE updates SET notified_at = ?
                   WHERE container_name = ? AND image = ? AND new_version = ? AND update_type = ?
                     AND notified_at IS NULL""",
                (ts, u.container_name, u.image, u.new_version, u.update_type),
            )
        conn.commit()
    finally:
        conn.close()


def get_active_updates() -> list[dict]:
    """Return all non-resolved update rows as dicts."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM updates WHERE resolved_at IS NULL ORDER BY first_seen_at"
        ).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()
