"""Unit tests for app.state — SQLite state persistence."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

import app.state as state
from app.models import UpdateInfo

# Pattern used by _make_update versions (major.minor.patch)
_PAT = r"^(\d+)\.(\d+)\.(\d+)$"


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
        # Container was updated to 1.1.0 → entry resolved
        cv = {("web", "nginx"): ("1.1.0", _PAT)}
        result2 = state.process_scan([], scan_time=t2, current_versions=cv)

        # Should be resolved
        assert len(result2) == 1
        assert result2[0].status == "resolved"
        assert len(state.get_active_updates()) == 0

        # User downgrades back to 1.0.0, update re-appears — re-opened as "known"
        result3 = state.process_scan([u], scan_time=t3)
        active = [r for r in result3 if r.status != "resolved"]
        assert len(active) == 1
        assert active[0].status == "known"


class TestResolve:
    def test_resolve_when_container_updated(self):
        """Entry is resolved when the container's current version >= new_version."""
        u = _make_update(new_version="1.1.0", update_type="minor")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        # Container updated to 1.1.0 — no more updates reported
        cv = {("web", "nginx"): ("1.1.0", _PAT)}
        result = state.process_scan([], scan_time=t2, current_versions=cv)

        resolved = [r for r in result if r.status == "resolved"]
        assert len(resolved) == 1
        assert resolved[0].new_version == "1.1.0"
        assert len(state.get_active_updates()) == 0

    def test_resolve_when_container_updated_past_new_version(self):
        """Entry is resolved when the container jumped past the reported version."""
        u = _make_update(new_version="1.1.0", update_type="minor")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        # Container updated to 1.2.0, skipping 1.1.0
        cv = {("web", "nginx"): ("1.2.0", _PAT)}
        result = state.process_scan([], scan_time=t2, current_versions=cv)

        resolved = [r for r in result if r.status == "resolved"]
        assert len(resolved) == 1

    def test_delete_when_version_yanked(self):
        """Entry is deleted when the upstream version no longer exists."""
        u = _make_update(new_version="1.1.0", update_type="minor")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        # Container still at 1.0.0, but 1.1.0 was yanked — scanner finds no updates
        cv = {("web", "nginx"): ("1.0.0", _PAT)}
        result = state.process_scan([], scan_time=t2, current_versions=cv)

        # Entry should be deleted, not resolved
        assert len(result) == 0
        assert len(state.get_active_updates()) == 0
        assert len(state.get_all_updates()) == 0

    def test_delete_when_version_superseded(self):
        """Old entry is deleted when a newer version replaces it."""
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(new_version="1.2.0", update_type="minor")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1], scan_time=t1)
        # 1.1.0 superseded by 1.2.0 — scanner now reports 1.2.0 instead
        cv = {("web", "nginx"): ("1.0.0", _PAT)}
        result = state.process_scan([u2], scan_time=t2, current_versions=cv)

        # 1.1.0 deleted, 1.2.0 is new
        new = [r for r in result if r.status == "new"]
        assert len(new) == 1
        assert new[0].new_version == "1.2.0"
        assert len(state.get_all_updates()) == 1

    def test_absent_entry_left_alone_when_container_not_scanned(self):
        """Entries for containers not in current_versions are untouched."""
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        # Empty scan with no version info (e.g. container temporarily unreachable)
        result = state.process_scan([], scan_time=t2)

        # Entry stays active — not resolved, not deleted
        active = [r for r in result if r.status == "known"]
        assert len(active) == 1
        assert len(state.get_active_updates()) == 1

    def test_resolve_one_delete_another(self):
        """Mixed: one container updated (resolved), another version yanked (deleted)."""
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(container_name="db", image="postgres",
                          current_version="15.0.0", new_version="15.1.0",
                          update_type="patch")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1, u2], scan_time=t1)

        cv = {
            ("web", "nginx"): ("1.1.0", _PAT),       # updated
            ("db", "postgres"): ("15.0.0", _PAT),     # still old → version yanked
        }
        result = state.process_scan([], scan_time=t2, current_versions=cv)

        resolved = [r for r in result if r.status == "resolved"]
        assert len(resolved) == 1
        assert resolved[0].container_name == "web"
        # db entry deleted
        assert len(state.get_all_updates()) == 1


class TestQueryByStatus:
    def test_get_active_excludes_resolved(self):
        u = _make_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        cv = {("web", "nginx"): ("1.1.0", _PAT)}
        state.process_scan([], scan_time=t2, current_versions=cv)

        assert len(state.get_active_updates()) == 0

    def test_process_scan_returns_all_statuses(self):
        u1 = _make_update(new_version="1.1.0", update_type="minor")
        u2 = _make_update(new_version="2.0.0", update_type="major")
        u3 = _make_update(container_name="cache", image="redis",
                          new_version="7.2.0", update_type="minor")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1, u2], scan_time=t1)
        # web updated to 2.0.0 (resolves major entry), cache is new
        cv = {("web", "nginx"): ("2.0.0", _PAT),
              ("cache", "redis"): ("7.0.0", _PAT)}
        result = state.process_scan([u1, u3], scan_time=t2, current_versions=cv)

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
        cv = {("web", "nginx"): ("1.1.0", _PAT)}
        state.process_scan([], scan_time=t2, current_versions=cv)

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
        # web updated to 2.0.0: resolves major, minor still active
        cv = {("web", "nginx"): ("2.0.0", _PAT)}
        state.process_scan([u1], scan_time=t2, current_versions=cv)

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
        # Resolve the update — container was updated
        cv = {("web", "nginx"): ("1.1.0", _PAT)}
        state.process_scan([], scan_time=t2, current_versions=cv)

        all_updates = state.get_all_updates()
        assert len(all_updates) == 1
        assert all_updates[0]["status"] == "resolved"


class TestDigestDeduplication:
    def _make_digest_update(self, **overrides) -> UpdateInfo:
        defaults = dict(
            container_name="app",
            service_name="app",
            stack="mystack",
            image="ghcr.io/example/app",
            current_version="dev",
            new_version="sha-aaaaaa",
            update_type="digest",
        )
        defaults.update(overrides)
        return UpdateInfo(**defaults)

    def test_second_digest_replaces_first(self):
        """A second digest change replaces the first entry rather than accumulating."""
        u1 = self._make_digest_update(new_version="sha-aaaaaa")
        u2 = self._make_digest_update(new_version="sha-bbbbbb")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1], scan_time=t1)
        state.process_scan([u2], scan_time=t2)

        active = state.get_active_updates()
        assert len(active) == 1
        assert active[0]["new_version"] == "sha-bbbbbb"
        assert active[0]["current_version"] == "dev"

    def test_second_digest_is_new_status(self):
        """A new digest hash is reported as 'new' even though the tag was already known."""
        u1 = self._make_digest_update(new_version="sha-aaaaaa")
        u2 = self._make_digest_update(new_version="sha-bbbbbb")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1], scan_time=t1)
        result = state.process_scan([u2], scan_time=t2)

        new_entries = [r for r in result if r.status == "new"]
        assert len(new_entries) == 1
        assert new_entries[0].new_version == "sha-bbbbbb"

    def test_multiple_digest_changes_stay_single_entry(self):
        """Three successive digest changes still produce only one row."""
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)
        t3 = datetime(2026, 1, 3, tzinfo=timezone.utc)

        state.process_scan([self._make_digest_update(new_version="sha-aaaaaa")], scan_time=t1)
        state.process_scan([self._make_digest_update(new_version="sha-bbbbbb")], scan_time=t2)
        state.process_scan([self._make_digest_update(new_version="sha-cccccc")], scan_time=t3)

        active = state.get_active_updates()
        assert len(active) == 1
        assert active[0]["new_version"] == "sha-cccccc"

    def test_same_digest_re_detected_stays_known(self):
        """When the same digest hash is re-detected the status remains 'known'."""
        u = self._make_digest_update(new_version="sha-aaaaaa")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u], scan_time=t1)
        result = state.process_scan([u], scan_time=t2)

        assert len(result) == 1
        assert result[0].status == "known"

    def test_digest_change_resets_notified_at(self):
        """A new digest hash resets notified_at so a fresh notification is sent."""
        u1 = self._make_digest_update(new_version="sha-aaaaaa")
        u2 = self._make_digest_update(new_version="sha-bbbbbb")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        result1 = state.process_scan([u1], scan_time=t1)
        state.mark_notified(result1, notified_time=t1)
        state.process_scan([u2], scan_time=t2)

        active = state.get_active_updates()
        assert len(active) == 1
        assert active[0]["notified_at"] is None

    def test_get_all_updates_shows_only_latest_digest(self):
        """get_all_updates never returns more than one digest entry per container+tag."""
        u1 = self._make_digest_update(new_version="sha-aaaaaa")
        u2 = self._make_digest_update(new_version="sha-bbbbbb")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.process_scan([u1], scan_time=t1)
        state.process_scan([u2], scan_time=t2)

        all_updates = state.get_all_updates()
        digest_entries = [u for u in all_updates if u["update_type"] == "digest"]
        assert len(digest_entries) == 1
        assert digest_entries[0]["new_version"] == "sha-bbbbbb"


class TestDigestAutoResolve:
    """Digest update is resolved when the container repulls and runs the updated image."""

    def _make_digest_update(self, **overrides) -> UpdateInfo:
        defaults = dict(
            container_name="app",
            service_name="app",
            stack="mystack",
            image="nginx",
            current_version="latest",
            new_version="sha256:newdigest111",
            update_type="digest",
        )
        defaults.update(overrides)
        return UpdateInfo(**defaults)

    def test_digest_resolved_when_container_repulled(self):
        """Pending digest update is resolved when RepoDigests contains the new digest."""
        u = self._make_digest_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.store_digest("nginx", "latest", "sha256:newdigest111")
        state.process_scan([u], scan_time=t1)

        cv = {("app", "nginx"): ("latest", r"^(\d+)\.(\d+)\.(\d+)$")}
        rd = {("app", "nginx"): ["nginx@sha256:newdigest111"]}
        result = state.process_scan([], scan_time=t2, current_versions=cv, running_digests=rd)

        resolved = [r for r in result if r.status == "resolved"]
        assert len(resolved) == 1
        assert resolved[0].new_version == "sha256:newdigest111"
        assert len(state.get_active_updates()) == 0

    def test_digest_not_resolved_when_container_has_old_image(self):
        """Update stays active when RepoDigests does not contain the new digest."""
        u = self._make_digest_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.store_digest("nginx", "latest", "sha256:newdigest111")
        state.process_scan([u], scan_time=t1)

        cv = {("app", "nginx"): ("latest", r"^(\d+)\.(\d+)\.(\d+)$")}
        rd = {("app", "nginx"): ["nginx@sha256:olddigest000"]}
        result = state.process_scan([], scan_time=t2, current_versions=cv, running_digests=rd)

        active = [r for r in result if r.status == "known"]
        assert len(active) == 1
        assert len(state.get_active_updates()) == 1

    def test_digest_not_resolved_without_running_digests(self):
        """Without running_digests, digest update is not resolved (backward compat)."""
        u = self._make_digest_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.store_digest("nginx", "latest", "sha256:newdigest111")
        state.process_scan([u], scan_time=t1)

        cv = {("app", "nginx"): ("latest", r"^(\d+)\.(\d+)\.(\d+)$")}
        result = state.process_scan([], scan_time=t2, current_versions=cv)

        active = [r for r in result if r.status == "known"]
        assert len(active) == 1

    def test_digest_not_resolved_with_empty_repo_digests(self):
        """Empty RepoDigests list leaves the update active."""
        u = self._make_digest_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.store_digest("nginx", "latest", "sha256:newdigest111")
        state.process_scan([u], scan_time=t1)

        cv = {("app", "nginx"): ("latest", r"^(\d+)\.(\d+)\.(\d+)$")}
        rd = {("app", "nginx"): []}
        result = state.process_scan([], scan_time=t2, current_versions=cv, running_digests=rd)

        active = [r for r in result if r.status == "known"]
        assert len(active) == 1

    def test_digest_resolved_entry_removed_from_active(self):
        """Resolved digest entry no longer appears in get_active_updates()."""
        u = self._make_digest_update()
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.store_digest("nginx", "latest", "sha256:newdigest111")
        state.process_scan([u], scan_time=t1)
        assert len(state.get_active_updates()) == 1

        cv = {("app", "nginx"): ("latest", r"^(\d+)\.(\d+)\.(\d+)$")}
        rd = {("app", "nginx"): ["nginx@sha256:newdigest111"]}
        state.process_scan([], scan_time=t2, current_versions=cv, running_digests=rd)

        assert len(state.get_active_updates()) == 0
        all_updates = state.get_all_updates()
        assert len(all_updates) == 1
        assert all_updates[0]["status"] == "resolved"

    def test_digest_resolved_with_resolved_version_as_new_version(self):
        """Works when new_version is a tag name (resolved via _resolve_digest_to_tag)."""
        u = self._make_digest_update(new_version="1.2.0")
        t1 = datetime(2026, 1, 1, tzinfo=timezone.utc)
        t2 = datetime(2026, 1, 2, tzinfo=timezone.utc)

        state.store_digest("nginx", "latest", "sha256:newdigest111")
        state.process_scan([u], scan_time=t1)

        cv = {("app", "nginx"): ("latest", r"^(\d+)\.(\d+)\.(\d+)$")}
        rd = {("app", "nginx"): ["nginx@sha256:newdigest111"]}
        result = state.process_scan([], scan_time=t2, current_versions=cv, running_digests=rd)

        resolved = [r for r in result if r.status == "resolved"]
        assert len(resolved) == 1
        assert resolved[0].new_version == "1.2.0"


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
