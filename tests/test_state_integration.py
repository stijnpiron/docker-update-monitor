"""Integration test: two consecutive scans produce correct state transitions."""

from datetime import datetime, timezone

import pytest

import app.state as state
from app.models import UpdateInfo

# Pattern used for version matching (major.minor.patch)
_PAT = r"^(\d+)\.(\d+)\.(\d+)$"
# Pattern for 2-group versions (major.minor)
_PAT2 = r"^(\d+)\.(\d+)$"


def test_two_consecutive_scans():
    """Simulate two scans: first introduces updates, second resolves one and adds another."""
    t1 = datetime(2026, 4, 1, 12, 0, 0, tzinfo=timezone.utc)
    t2 = datetime(2026, 4, 1, 13, 0, 0, tzinfo=timezone.utc)

    update_a = UpdateInfo(
        container_name="web",
        service_name="web",
        stack="mystack",
        image="nginx",
        current_version="1.0.0",
        new_version="1.1.0",
        update_type="minor",
    )
    update_b = UpdateInfo(
        container_name="db",
        service_name="db",
        stack="mystack",
        image="postgres",
        current_version="15.0.0",
        new_version="15.1.0",
        update_type="patch",
    )
    update_c = UpdateInfo(
        container_name="cache",
        service_name="cache",
        stack="mystack",
        image="redis",
        current_version="7.0.0",
        new_version="7.2.0",
        update_type="minor",
    )

    # --- Scan 1: A and B are new ---
    result1 = state.process_scan([update_a, update_b], scan_time=t1)

    new1 = [r for r in result1 if r.status == "new"]
    assert len(new1) == 2
    assert len(result1) == 2  # no resolved yet

    active = state.get_active_updates()
    assert len(active) == 2
    for row in active:
        assert row["first_seen_at"] == t1.isoformat()
        assert row["last_seen_at"] == t1.isoformat()
        assert row["resolved_at"] is None
        assert row["notified_at"] is None

    # Mark as notified
    state.mark_notified(result1, notified_time=t1)

    # --- Scan 2: A still present, B resolved (db updated), C is new ---
    cv = {
        ("web", "nginx"): ("1.0.0", _PAT),
        ("db", "postgres"): ("15.1.0", _PAT),      # db updated to 15.1.0
        ("cache", "redis"): ("7.0.0", _PAT),
    }
    result2 = state.process_scan([update_a, update_c], scan_time=t2, current_versions=cv)

    new2 = [r for r in result2 if r.status == "new"]
    known2 = [r for r in result2 if r.status == "known"]
    resolved2 = [r for r in result2 if r.status == "resolved"]

    # C is new, A is known, B is resolved
    assert len(new2) == 1
    assert new2[0].container_name == "cache"
    assert len(known2) == 1
    assert known2[0].container_name == "web"
    assert len(resolved2) == 1
    assert resolved2[0].container_name == "db"

    active = state.get_active_updates()
    assert len(active) == 2
    active_names = {r["container_name"] for r in active}
    assert active_names == {"web", "cache"}

    # Check A: known — first_seen < last_seen
    row_a = next(r for r in active if r["container_name"] == "web")
    assert row_a["first_seen_at"] == t1.isoformat()
    assert row_a["last_seen_at"] == t2.isoformat()
    assert row_a["notified_at"] == t1.isoformat()

    # Check C: new — first_seen == last_seen == t2
    row_c = next(r for r in active if r["container_name"] == "cache")
    assert row_c["first_seen_at"] == t2.isoformat()
    assert row_c["last_seen_at"] == t2.isoformat()
    assert row_c["notified_at"] is None
