import sqlite3
from copy import copy
from datetime import datetime, timezone
from pathlib import Path

import app.config as _config
from app.migrations import run_migrations
from app.models import UpdateInfo
from app.version import parse_tag

_DB_PATH = Path(_config.STATE_DB_PATH)

_SCHEMA = """\
CREATE TABLE IF NOT EXISTS updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    container_name TEXT NOT NULL,
    service_name TEXT NOT NULL DEFAULT '',
    image TEXT NOT NULL,
    current_version TEXT NOT NULL,
    new_version TEXT NOT NULL,
    update_type TEXT NOT NULL,
    stack TEXT NOT NULL,
    first_seen_at TEXT NOT NULL,
    last_seen_at TEXT NOT NULL,
    notified_at TEXT,
    resolved_at TEXT,
    UNIQUE(container_name, image, current_version, update_type)
);
"""

_DIGESTS_SCHEMA = """\
CREATE TABLE IF NOT EXISTS digests (
    image TEXT NOT NULL,
    tag TEXT NOT NULL,
    digest TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (image, tag)
);
"""

_METADATA_SCHEMA = """\
CREATE TABLE IF NOT EXISTS metadata (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL
);
"""


def _connect() -> sqlite3.Connection:
    _DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(_DB_PATH))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute(_SCHEMA)
    conn.execute(_DIGESTS_SCHEMA)
    conn.execute(_METADATA_SCHEMA)
    run_migrations(conn)
    conn.commit()
    return conn


def process_scan(
    updates: list[UpdateInfo],
    scan_time: datetime | None = None,
    current_versions: dict[tuple[str, str], tuple[str, str]] | None = None,
) -> list[UpdateInfo]:
    """Upsert scan results, resolve or delete absent entries, return updates with status.

    Returns a list of UpdateInfo with ``status`` set to ``"new"``, ``"known"``,
    or ``"resolved"``.

    *current_versions* maps ``(container_name, image)`` → ``(current_tag, pattern)``
    for every container that was successfully scanned this cycle.  For absent DB
    entries whose container appears in this map:

    * If the container's current version ≥ the stored ``new_version`` → **resolved**
      (the user updated the container).
    * Otherwise → **deleted** (the upstream version was yanked / superseded).

    Entries for containers *not* in *current_versions* are left untouched (the
    container may have been temporarily unreachable).
    """
    if scan_time is None:
        scan_time = datetime.now(timezone.utc)
    if current_versions is None:
        current_versions = {}

    ts = scan_time.isoformat()
    conn = _connect()
    try:
        # --- upsert current findings ---
        for u in updates:
            conn.execute(
                """INSERT INTO updates (container_name, service_name, image, current_version, new_version,
                                        update_type, stack, first_seen_at, last_seen_at, resolved_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, NULL)
                   ON CONFLICT(container_name, image, current_version, update_type) DO UPDATE SET
                       new_version = excluded.new_version,
                       last_seen_at = excluded.last_seen_at,
                       service_name = excluded.service_name,
                       stack = excluded.stack,
                       resolved_at = NULL,
                       notified_at = CASE WHEN excluded.new_version != updates.new_version THEN NULL ELSE updates.notified_at END,
                       first_seen_at = CASE WHEN excluded.new_version != updates.new_version THEN excluded.first_seen_at ELSE updates.first_seen_at END""",
                (u.container_name, u.service_name, u.image, u.current_version, u.new_version,
                 u.update_type, u.stack, ts, ts),
            )

        # --- resolve or delete absent entries ---
        current_keys = {
            (u.container_name, u.image, u.current_version, u.update_type)
            for u in updates
        }

        conn.row_factory = sqlite3.Row
        active_rows = conn.execute(
            "SELECT * FROM updates WHERE resolved_at IS NULL",
        ).fetchall()

        resolved_ids: list[int] = []

        for row in active_rows:
            key = (row["container_name"], row["image"], row["current_version"], row["update_type"])
            if key in current_keys:
                continue  # still detected, already upserted

            cv_key = (row["container_name"], row["image"])
            if cv_key not in current_versions:
                continue  # container not scanned this cycle — leave entry alone

            # Container was scanned — check if it was updated
            current_tag, pattern = current_versions[cv_key]

            if row["update_type"] == "digest":
                # For digest updates there is no semver ordering to compare.
                # If the rolling tag changed (e.g. :edge → :dev) the old entry
                # is obsolete — discard it.  If the tag is unchanged we cannot
                # reliably tell whether the container was restarted with the new
                # image, so leave the entry alone until the next digest change.
                if current_tag != row["current_version"]:
                    conn.execute("DELETE FROM updates WHERE id = ?", (row["id"],))
                continue

            resolved = False
            try:
                current_parsed = parse_tag(current_tag, pattern)
                new_parsed = parse_tag(row["new_version"], pattern)
                if current_parsed is not None and new_parsed is not None and current_parsed >= new_parsed:
                    resolved = True
            except (ValueError, TypeError):
                pass

            if resolved:
                conn.execute(
                    "UPDATE updates SET resolved_at = ? WHERE id = ?",
                    (ts, row["id"]),
                )
                resolved_ids.append(row["id"])
            else:
                # Version yanked or superseded — forget it
                conn.execute("DELETE FROM updates WHERE id = ?", (row["id"],))

        conn.commit()

        # --- build categorized result ---
        if resolved_ids:
            ph = ",".join("?" * len(resolved_ids))
            resolved_rows = conn.execute(
                f"SELECT * FROM updates WHERE id IN ({ph}) ORDER BY first_seen_at",
                resolved_ids,
            ).fetchall()
        else:
            resolved_rows = []

        active_rows = conn.execute(
            "SELECT * FROM updates WHERE resolved_at IS NULL ORDER BY first_seen_at",
        ).fetchall()

        result: list[UpdateInfo] = []

        for row in active_rows:
            status = "new" if row["first_seen_at"] == ts else "known"
            result.append(UpdateInfo(
                container_name=row["container_name"],
                service_name=row["service_name"],
                stack=row["stack"],
                image=row["image"],
                current_version=row["current_version"],
                new_version=row["new_version"],
                update_type=row["update_type"],
                status=status,
                first_seen_at=row["first_seen_at"],
            ))

        for row in resolved_rows:
            result.append(UpdateInfo(
                container_name=row["container_name"],
                service_name=row["service_name"],
                stack=row["stack"],
                image=row["image"],
                current_version=row["current_version"],
                new_version=row["new_version"],
                update_type=row["update_type"],
                status="resolved",
                first_seen_at=row["first_seen_at"],
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


def get_all_updates() -> list[dict]:
    """Return all update rows (active and resolved) as dicts with a 'status' field."""
    conn = _connect()
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT * FROM updates ORDER BY first_seen_at"
        ).fetchall()
        result = []
        for row in rows:
            d = dict(row)
            if d.get("resolved_at"):
                d["status"] = "resolved"
            elif d.get("notified_at"):
                d["status"] = "known"
            else:
                d["status"] = "new"
            result.append(d)
        return result
    finally:
        conn.close()


def save_last_check(iso_timestamp: str) -> None:
    """Persist the last scan timestamp to the database."""
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO metadata (key, value) VALUES ('last_check', ?)",
            (iso_timestamp,),
        )
        conn.commit()
    finally:
        conn.close()


def load_last_check() -> str | None:
    """Load the persisted last scan timestamp from the database."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT value FROM metadata WHERE key = 'last_check'"
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Digest tracking
# ---------------------------------------------------------------------------

def get_stored_digest(image: str, tag: str) -> str | None:
    """Return the previously stored digest for (image, tag), or None."""
    conn = _connect()
    try:
        row = conn.execute(
            "SELECT digest FROM digests WHERE image = ? AND tag = ?",
            (image, tag),
        ).fetchone()
        return row[0] if row else None
    finally:
        conn.close()


def store_digest(image: str, tag: str, digest: str, timestamp: datetime | None = None) -> None:
    """Insert or update the stored digest for (image, tag)."""
    if timestamp is None:
        timestamp = datetime.now(timezone.utc)
    ts = timestamp.isoformat()
    conn = _connect()
    try:
        conn.execute(
            "INSERT OR REPLACE INTO digests (image, tag, digest, updated_at) VALUES (?, ?, ?, ?)",
            (image, tag, digest, ts),
        )
        conn.commit()
    finally:
        conn.close()
