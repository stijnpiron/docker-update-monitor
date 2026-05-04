"""Unit tests for app.state — SQLite state persistence."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import app.state as state
from app.models import UpdateInfo


def _make_update(**overrides) -> UpdateInfo:
    defaults = dict(
        container_name="web",
        service_name="web",
        stack="mystack",
        image="nginx",
        current_version="1.0.0",
        new_version="1.1.0",
        update_type="minor",
    )
    defaults.update(overrides)
    return UpdateInfo(**defaults)


class TestInsert:
    def test_insert_new_update(self):
        u = _make_update()
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        result = state.process_scan([u], scan_time=t)

        assert len(result) == 1
        assert result[0].container_name == "web"
        assert result[0].status == "new"

    def test_insert_sets_first_and_last_seen(self):
        u = _make_update()
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t)
        rows = state.get_active_updates()

        assert len(rows) == 1
        assert rows[0]["first_seen_at"] == t.isoformat()
        assert rows[0]["last_seen_at"] == t.isoformat()
        assert rows[0]["notified_at"] is None

    def test_insert_multiple_updates(self):
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(new_version="2.0.0", update_type="major")
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        result = state.process_scan([u1, u2], scan_time=t)
        new = [r for r in result if r.status == "new"]
        assert len(new) == 2


class TestUpsert:
    def test_upsert_updates_last_seen(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        result = state.process_scan([u], scan_time=t2)

        # Second scan should return as known, not new
        assert len(result) == 1
        assert result[0].status == "known"

        rows = state.get_active_updates()
        assert rows[0]["first_seen_at"] == t1.isoformat()
        assert rows[0]["last_seen_at"] == t2.isoformat()

    def test_upsert_re_opens_resolved(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 3, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        result2 = state.process_scan([], scan_time=t2)

        # Should be resolved
        assert len(result2) == 1
        assert result2[0].status == "resolved"
        assert len(state.get_active_updates()) == 0

        # Re-appear in scan — was seen before so it's "known"
        result3 = state.process_scan([u], scan_time=t3)
        active = [r for r in result3 if r.status != "resolved"]
        assert len(active) == 1
        assert active[0].status == "known"


class TestResolve:
    def test_resolve_absent_marks_missing(self):
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(new_version="2.0.0", update_type="major")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1, u2], scan_time=t1)
        # Second scan only has u1 — u2 should be resolved
        result = state.process_scan([u1], scan_time=t2)

        known = [r for r in result if r.status == "known"]
        resolved = [r for r in result if r.status == "resolved"]
        assert len(known) == 1
        assert known[0].new_version == "1.1.0"
        assert len(resolved) == 1
        assert resolved[0].new_version == "2.0.0"

    def test_resolve_all_when_empty_scan(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        result = state.process_scan([], scan_time=t2)

        assert len(result) == 1
        assert result[0].status == "resolved"
        assert len(state.get_active_updates()) == 0


class TestQueryByStatus:
    def test_get_active_excludes_resolved(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        state.process_scan([], scan_time=t2)

        assert len(state.get_active_updates()) == 0

    def test_process_scan_returns_all_statuses(self):
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(new_version="2.0.0", update_type="major")
        u3 = _make_update(container_name="cache", image="redis",
                          new_version="7.2.0", update_type="minor")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1, u2], scan_time=t1)
        result = state.process_scan([u1, u3], scan_time=t2)

        statuses = {r.status for r in result}
        assert statuses == {"new", "known", "resolved"}


class TestMarkNotified:
    def test_mark_notified_sets_timestamp(self):
        u = _make_update()
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        result = state.process_scan([u], scan_time=t)
        state.mark_notified(result, notified_time=t)

        rows = state.get_active_updates()
        assert rows[0]["notified_at"] == t.isoformat()

    def test_mark_notified_idempotent(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        result = state.process_scan([u], scan_time=t1)
        state.mark_notified(result, notified_time=t1)
        state.mark_notified(result, notified_time=t2)

        rows = state.get_active_updates()
        # Should keep the first notified_at
        assert rows[0]["notified_at"] == t1.isoformat()


class TestProcessScanDefaultTime:
    def test_process_scan_uses_current_time_when_none(self):
        u = _make_update()
        result = state.process_scan([u])
        assert len(result) == 1
        assert result[0].status == "new"


class TestMarkNotifiedDefaultTime:
    def test_mark_notified_uses_current_time_when_none(self):
        u = _make_update()
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = state.process_scan([u], scan_time=t)
        state.mark_notified(result)
        rows = state.get_active_updates()
        assert rows[0]["notified_at"] is not None


class TestGetAllUpdates:
    def test_returns_empty_list_when_no_data(self):
        result = state.get_all_updates()
        assert result == []

    def test_returns_new_update(self):
        u = _make_update()
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        state.process_scan([u], scan_time=t)

        result = state.get_all_updates()
        assert len(result) == 1
        assert result[0]["status"] == "new"
        assert result[0]["container_name"] == "web"

    def test_returns_known_update(self):
        u = _make_update()
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)
        result = state.process_scan([u], scan_time=t)
        state.mark_notified(result, notified_time=t)

        all_updates = state.get_all_updates()
        assert len(all_updates) == 1
        assert all_updates[0]["status"] == "known"

    def test_returns_resolved_update(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        state.process_scan([u], scan_time=t1)
        state.process_scan([], scan_time=t2)

        all_updates = state.get_all_updates()
        assert len(all_updates) == 1
        assert all_updates[0]["status"] == "resolved"

    def test_returns_multiple_statuses(self):
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(new_version="2.0.0", update_type="major")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        result = state.process_scan([u1, u2], scan_time=t1)
        state.mark_notified(result, notified_time=t1)
        state.process_scan([u1], scan_time=t2)

        all_updates = state.get_all_updates()
        statuses = {u["status"] for u in all_updates}
        assert statuses == {"known", "resolved"}

    def test_resolved_status_takes_precedence_over_notified(self):
        """An update that was notified and then resolved should show as 'resolved'."""
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        result = state.process_scan([u], scan_time=t1)
        state.mark_notified(result, notified_time=t1)
        # Resolve the update
        state.process_scan([], scan_time=t2)

        all_updates = state.get_all_updates()
        assert len(all_updates) == 1
        assert all_updates[0]["status"] == "resolved"


class TestProcessScanEdgeCases:
    def test_empty_container_name(self):
        """Update with empty container_name is stored and retrieved."""
        u = _make_update(container_name="")
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        result = state.process_scan([u], scan_time=t)
        assert len(result) == 1
        assert result[0].container_name == ""

    def test_empty_image(self):
        """Update with empty image is stored and retrieved."""
        u = _make_update(image="")
        t = datetime(2026, 1, 1, tzinfo=timezone.utc)

        result = state.process_scan([u], scan_time=t)
        assert len(result) == 1
        assert result[0].image == ""
