"""Unit tests for the Flask dashboard routes."""

import contextlib
import json
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock

import pytest

from app import config as config_mod
from app.dashboard import create_app, _scan_trigger, _format_datetime


@pytest.fixture
def client():
    """Create a Flask test client."""
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


@pytest.fixture(autouse=True)
def _reset_scan_trigger():
    """Ensure scan trigger is cleared between tests."""
    _scan_trigger.clear()
    yield
    _scan_trigger.clear()


def _snapshot_and_restore_health_state():
    """Generator: snapshot app.health._state, yield, then restore it.

    Why: Several tests mutate `_state` to exercise dashboard rendering. Without
    a yield-based teardown, an assertion failure leaves the global polluted for
    every subsequent test (see issue #122). Extracted from the fixture so the
    regression tests in TestStateCleanupOnFailure can drive it directly.
    """
    from app.health import _state, _state_lock
    with _state_lock:
        snapshot = {k: list(v) if isinstance(v, list) else v for k, v in _state.items()}
    try:
        yield
    finally:
        with _state_lock:
            _state.clear()
            _state.update(snapshot)


@pytest.fixture(autouse=True)
def _reset_health_state():
    yield from _snapshot_and_restore_health_state()


class TestDashboardRoute:
    """Tests for GET /"""

    @patch("app.dashboard.get_all_updates")
    def test_renders_dashboard(self, mock_updates, client):
        mock_updates.return_value = [
            {
                "container_name": "nginx-web",
                "service_name": "web",
                "stack": "mystack",
                "image": "nginx",
                "current_version": "1.24.0",
                "new_version": "1.25.0",
                "update_type": "minor",
                "status": "new",
                "first_seen_at": "2026-04-30T10:00:00",
            }
        ]
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "Docker Update Monitor" in html
        assert "nginx-web" in html
        assert "mystack" in html
        assert "1.24.0" in html
        assert "1.25.0" in html

    @patch("app.dashboard.get_all_updates")
    def test_empty_state_renders(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/")
        assert resp.status_code == 200
        html = resp.data.decode()
        assert "No updates found" in html

    @patch("app.dashboard.get_all_updates")
    def test_summary_cards_counts(self, mock_updates, client):
        mock_updates.return_value = [
            {"status": "new", "container_name": "a", "service_name": "", "stack": "s", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "first_seen_at": "2026-04-30T10:00:00"},
            {"status": "new", "container_name": "b", "service_name": "", "stack": "s", "image": "img2", "current_version": "1.0", "new_version": "1.1", "update_type": "minor", "first_seen_at": "2026-04-30T10:00:00"},
            {"status": "known", "container_name": "c", "service_name": "", "stack": "s", "image": "img3", "current_version": "1.0", "new_version": "1.0.1", "update_type": "patch", "first_seen_at": "2026-04-30T09:00:00"},
            {"status": "resolved", "container_name": "d", "service_name": "", "stack": "s", "image": "img4", "current_version": "1.0", "new_version": "1.1", "update_type": "minor", "first_seen_at": "2026-04-30T08:00:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        # New count = 2, Known = 1, Resolved = 1
        assert ">2<" in html  # new count
        assert ">1<" in html  # known and resolved

    @patch("app.dashboard.get_all_updates")
    def test_new_updates_highlighted(self, mock_updates, client):
        mock_updates.return_value = [
            {"status": "new", "container_name": "a", "service_name": "", "stack": "s", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "first_seen_at": "2026-04-30T10:00:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "status-new" in html

    @patch("app.dashboard.get_all_updates")
    def test_resolved_updates_greyed(self, mock_updates, client):
        mock_updates.return_value = [
            {"status": "resolved", "container_name": "a", "service_name": "", "stack": "s", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "first_seen_at": "2026-04-30T10:00:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "status-resolved" in html

    @patch("app.dashboard.get_all_updates")
    def test_xss_prevention(self, mock_updates, client):
        mock_updates.return_value = [
            {"status": "new", "container_name": "<script>alert(1)</script>", "service_name": "", "stack": "s", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "first_seen_at": "2026-04-30T10:00:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "<script>alert(1)</script>" not in html
        assert "&lt;script&gt;" in html


class TestApiUpdatesRoute:
    """Tests for GET /api/updates"""

    @patch("app.dashboard.get_all_updates")
    def test_returns_json_array(self, mock_updates, client):
        mock_updates.return_value = [
            {"container_name": "test", "status": "new", "image": "nginx"}
        ]
        resp = client.get("/api/updates")
        assert resp.status_code == 200
        assert resp.content_type == "application/json"
        data = json.loads(resp.data)
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["container_name"] == "test"
        assert data[0]["status"] == "new"

    @patch("app.dashboard.get_all_updates")
    def test_empty_returns_empty_array(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/api/updates")
        data = json.loads(resp.data)
        assert data == []


class TestApiScanRoute:
    """Tests for POST /api/scan"""

    def test_returns_202_accepted(self, client):
        resp = client.post("/api/scan")
        assert resp.status_code == 202
        data = json.loads(resp.data)
        assert "message" in data
        assert "triggered" in data["message"].lower() or "Scan" in data["message"]

    def test_sets_scan_trigger_event(self, client):
        assert not _scan_trigger.is_set()
        client.post("/api/scan")
        assert _scan_trigger.is_set()

    def test_get_not_allowed(self, client):
        resp = client.get("/api/scan")
        assert resp.status_code == 405


class TestHealthRoute:
    """Tests for GET /health"""

    @patch("app.dashboard._build_response")
    def test_returns_health_ok(self, mock_build, client):
        mock_build.return_value = (200, {"status": "ok", "uptime_seconds": 42})
        resp = client.get("/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "ok"

    @patch("app.dashboard._build_response")
    def test_returns_200_starting_before_first_check(self, mock_build, client):
        mock_build.return_value = (200, {"status": "starting", "note": "waiting for first scan to complete"})
        resp = client.get("/health")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data["status"] == "starting"


class TestDatetimeFormatting:
    """Tests for _format_datetime and DASHBOARD_DATETIME_FORMAT."""

    def test_default_format(self):
        result = _format_datetime("2026-04-30T10:30:00")
        assert result == "30/04/2026 10:30"

    def test_custom_format(self):
        with patch.object(config_mod, "DASHBOARD_DATETIME_FORMAT", "%Y-%m-%d %H:%M:%S"):
            result = _format_datetime("2026-04-30T10:30:45")
        assert result == "2026-04-30 10:30:45"

    def test_none_returns_dash(self):
        assert _format_datetime(None) == "—"

    def test_empty_string_returns_dash(self):
        assert _format_datetime("") == "—"

    def test_invalid_iso_returns_raw(self):
        assert _format_datetime("not-a-date") == "not-a-date"

    @patch("app.dashboard.get_all_updates")
    def test_formatted_date_in_dashboard(self, mock_updates, client):
        mock_updates.return_value = [
            {"container_name": "app", "service_name": "", "stack": "s", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "status": "new", "first_seen_at": "2026-04-30T14:05:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "30/04/2026 14:05" in html

    def test_utc_timestamp_converted_to_configured_timezone(self):
        """A UTC timestamp with TZ=Europe/Brussels (UTC+2 in summer) shows +2h."""
        with patch.object(config_mod, "TZ", "Europe/Brussels"):
            result = _format_datetime("2026-04-30T08:00:00+00:00")
        assert result == "30/04/2026 10:00"

    def test_utc_timestamp_with_no_tz_uses_local(self):
        """Without TZ set, a UTC timestamp is converted to the system local timezone."""
        with patch.object(config_mod, "TZ", ""):
            dt_utc = datetime(2026, 4, 30, 8, 0, 0, tzinfo=timezone.utc)
            expected = dt_utc.astimezone().strftime(config_mod.DASHBOARD_DATETIME_FORMAT)
            result = _format_datetime("2026-04-30T08:00:00+00:00")
        assert result == expected

    def test_unknown_timezone_falls_back_to_local(self):
        """An unrecognised TZ value logs a warning and falls back to system local timezone."""
        with patch.object(config_mod, "TZ", "Invalid/Timezone"):
            dt_utc = datetime(2026, 4, 30, 8, 0, 0, tzinfo=timezone.utc)
            expected = dt_utc.astimezone().strftime(config_mod.DASHBOARD_DATETIME_FORMAT)
            with patch.object(config_mod.log, "warning") as mock_warn:
                result = _format_datetime("2026-04-30T08:00:00+00:00")
            mock_warn.assert_called_once()
            assert "Invalid/Timezone" in mock_warn.call_args[0][1]
        assert result == expected

    def test_naive_datetime_not_converted(self):
        """Naive datetimes (no tzinfo) are formatted as-is regardless of TZ."""
        with patch.object(config_mod, "TZ", "Europe/Brussels"):
            result = _format_datetime("2026-04-30T10:30:00")
        assert result == "30/04/2026 10:30"


class TestTableSorting:
    """Tests for default sort order (by stack)."""

    @patch("app.dashboard.get_all_updates")
    def test_sorted_by_stack_then_container(self, mock_updates, client):
        mock_updates.return_value = [
            {"container_name": "zzz", "service_name": "", "stack": "beta", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "status": "new", "first_seen_at": "2026-04-30T10:00:00"},
            {"container_name": "aaa", "service_name": "", "stack": "alpha", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "status": "new", "first_seen_at": "2026-04-30T10:00:00"},
            {"container_name": "bbb", "service_name": "", "stack": "alpha", "image": "img2", "current_version": "1.0", "new_version": "2.0", "update_type": "minor", "status": "known", "first_seen_at": "2026-04-30T09:00:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        # alpha should appear before beta
        alpha_pos = html.index("alpha")
        beta_pos = html.index("beta")
        assert alpha_pos < beta_pos
        # within alpha, aaa before bbb
        aaa_pos = html.index("aaa")
        bbb_pos = html.index("bbb")
        assert aaa_pos < bbb_pos

    @patch("app.dashboard.get_all_updates")
    def test_table_has_sortable_headers(self, mock_updates, client):
        mock_updates.return_value = [
            {"container_name": "app", "service_name": "", "stack": "s", "image": "img", "current_version": "1.0", "new_version": "2.0", "update_type": "major", "status": "new", "first_seen_at": "2026-04-30T10:00:00"},
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert 'data-col="0"' in html
        assert "sort-arrow" in html


class TestWarningsDisplay:
    """Tests for warnings shown on the dashboard."""

    @patch("app.dashboard.get_all_updates")
    def test_warnings_shown_when_present(self, mock_updates, client):
        mock_updates.return_value = []
        from app.health import _state, _state_lock
        with _state_lock:
            _state["warnings"] = [
                {"container_name": "broken", "image": "nginx", "level": "warning", "message": "Invalid tag-regex '(': missing )"},
            ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "Warnings" in html
        assert "broken" in html
        assert "Invalid tag-regex" in html

    @patch("app.dashboard.get_all_updates")
    def test_no_warnings_section_when_empty(self, mock_updates, client):
        mock_updates.return_value = []
        from app.health import _state, _state_lock
        with _state_lock:
            _state["warnings"] = []
        resp = client.get("/")
        html = resp.data.decode()
        assert '<table class="warnings-table">' not in html


class TestSkippedContainersDisplay:
    """Tests for not-monitored containers on the dashboard."""

    @patch("app.dashboard.get_all_updates")
    def test_skipped_containers_shown(self, mock_updates, client):
        mock_updates.return_value = []
        from app.health import _state, _state_lock
        with _state_lock:
            _state["skipped_containers"] = [
                {"container_name": "redis-cache", "stack": "infra", "image": "redis:7", "reason": "No 'docker-update-monitor.tag-regex' label"},
                {"container_name": "postgres-db", "stack": "app", "image": "postgres:16", "reason": "No 'docker-update-monitor.tag-regex' label"},
            ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "Not Monitored" in html
        assert "redis-cache" in html
        assert "postgres-db" in html
        assert "No &#39;docker-update-monitor.tag-regex&#39; label" in html

    @patch("app.dashboard.get_all_updates")
    def test_skipped_sorted_by_stack(self, mock_updates, client):
        mock_updates.return_value = []
        from app.health import _state, _state_lock
        with _state_lock:
            _state["skipped_containers"] = [
                {"container_name": "z-app", "stack": "zebra", "image": "img:1", "reason": "no label"},
                {"container_name": "a-app", "stack": "alpha", "image": "img:2", "reason": "no label"},
            ]
        resp = client.get("/")
        html = resp.data.decode()
        alpha_pos = html.index("alpha")
        zebra_pos = html.index("zebra")
        assert alpha_pos < zebra_pos

    @patch("app.dashboard.get_all_updates")
    def test_no_skipped_section_when_empty(self, mock_updates, client):
        mock_updates.return_value = []
        from app.health import _state, _state_lock
        with _state_lock:
            _state["skipped_containers"] = []
        resp = client.get("/")
        html = resp.data.decode()
        assert '<table class="skipped-table">' not in html


class TestApiLastScanRoute:
    """Tests for GET /api/last-scan"""

    def test_returns_null_before_first_scan(self, client):
        from app.health import _state, _state_lock
        with _state_lock:
            _state["last_check"] = None
        resp = client.get("/api/last-scan")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {"last_check": None}

    def test_returns_last_check_timestamp(self, client):
        from app.health import _state, _state_lock
        with _state_lock:
            _state["last_check"] = "2026-04-30T12:00:00Z"
        resp = client.get("/api/last-scan")
        assert resp.status_code == 200
        data = json.loads(resp.data)
        assert data == {"last_check": "2026-04-30T12:00:00Z"}

    def test_update_banner_in_dashboard_html(self, client):
        from unittest.mock import patch as _p
        with _p("app.dashboard.get_all_updates", return_value=[]):
            resp = client.get("/")
        html = resp.data.decode()
        assert 'id="update-banner"' in html
        assert "New scan results available" in html
        assert "data-last-check" in html
        assert "js/dashboard.js" in html


class TestStaticAssets:
    """Tests for external CSS and JS file references."""

    @patch("app.dashboard.get_all_updates")
    def test_css_referenced_via_url_for(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/")
        html = resp.data.decode()
        assert "css/dashboard.css" in html
        assert "<link" in html

    @patch("app.dashboard.get_all_updates")
    def test_js_referenced_with_defer(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/")
        html = resp.data.decode()
        assert "js/dashboard.js" in html
        assert "defer" in html

    @patch("app.dashboard.get_all_updates")
    def test_no_inline_style_block(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/")
        html = resp.data.decode()
        assert "<style>" not in html

    @patch("app.dashboard.get_all_updates")
    def test_no_jinja2_in_js(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/")
        html = resp.data.decode()
        assert "last_check_raw" not in html

    @patch("app.dashboard.get_all_updates")
    def test_data_last_check_on_body(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/")
        html = resp.data.decode()
        assert "data-last-check" in html

    @patch("app.dashboard.get_all_updates")
    def test_static_css_file_served(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/static/css/dashboard.css")
        assert resp.status_code == 200
        assert b"font-family" in resp.data

    @patch("app.dashboard.get_all_updates")
    def test_static_js_file_served(self, mock_updates, client):
        mock_updates.return_value = []
        resp = client.get("/static/js/dashboard.js")
        assert resp.status_code == 200
        assert b"makeSortable" in resp.data


class TestPendingResolvedSplit:
    """Tests for the pending/resolved updates split."""

    _base = {
        "service_name": "",
        "stack": "s",
        "image": "img",
        "current_version": "1.0",
        "new_version": "2.0",
        "update_type": "major",
        "first_seen_at": "2026-04-30T10:00:00",
    }

    def _update(self, **kwargs):
        return {**self._base, **kwargs}

    @patch("app.dashboard.get_all_updates")
    def test_pending_updates_in_primary_table(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="a", status="new"),
            self._update(container_name="b", status="known"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert 'id="pending-table"' in html
        assert "status-new" in html
        assert "status-known" in html

    @patch("app.dashboard.get_all_updates")
    def test_resolved_updates_in_details_element(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="c", status="resolved"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "<details" in html
        assert 'id="resolved-table"' in html
        assert "status-resolved" in html

    @patch("app.dashboard.get_all_updates")
    def test_resolved_summary_shows_count(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="c", status="resolved"),
            self._update(container_name="d", status="resolved"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "Resolved (2)" in html

    @patch("app.dashboard.get_all_updates")
    def test_empty_state_when_no_pending(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="c", status="resolved"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "No updates found" in html
        assert 'id="pending-table"' not in html

    @patch("app.dashboard.get_all_updates")
    def test_no_resolved_section_when_none(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="a", status="new"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert 'id="resolved-table"' not in html

    @patch("app.dashboard.get_all_updates")
    def test_pending_heading_shows_count(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="a", status="new"),
            self._update(container_name="b", status="known"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        assert "2 pending updates" in html

    @patch("app.dashboard.get_all_updates")
    def test_resolved_not_in_pending_table(self, mock_updates, client):
        mock_updates.return_value = [
            self._update(container_name="pending-one", status="new"),
            self._update(container_name="resolved-one", status="resolved"),
        ]
        resp = client.get("/")
        html = resp.data.decode()
        pending_pos = html.index('id="pending-table"')
        resolved_pos = html.index('id="resolved-table"')
        pending_section = html[pending_pos:resolved_pos]
        assert "resolved-one" not in pending_section


class TestStateCleanupOnFailure:
    """Regression tests for issue #122: _state cleanup must survive assertion failures."""

    def test_state_restored_when_test_raises(self):
        """Drive the _reset_health_state fixture manually and confirm it restores
        _state when the wrapped test body raises."""
        from app.health import _state, _state_lock

        with _state_lock:
            _state["warnings"] = [{"sentinel": "pre-existing"}]
            _state["skipped_containers"] = []

        gen = _snapshot_and_restore_health_state()
        next(gen)
        with _state_lock:
            _state["warnings"] = [{"polluted": "during-test"}]
            _state["skipped_containers"] = [{"polluted": "during-test"}]
        try:
            gen.throw(AssertionError("simulated failure"))
        except AssertionError:
            pass
        with contextlib.suppress(StopIteration):
            next(gen)

        with _state_lock:
            assert _state["warnings"] == [{"sentinel": "pre-existing"}]
            assert _state["skipped_containers"] == []
            _state["warnings"] = []

    def test_state_restored_on_clean_exit(self):
        """The fixture also restores state when the test body completes normally."""
        from app.health import _state, _state_lock

        with _state_lock:
            _state["warnings"] = [{"sentinel": "original"}]

        gen = _snapshot_and_restore_health_state()
        next(gen)
        with _state_lock:
            _state["warnings"] = [{"polluted": "yes"}]
        with contextlib.suppress(StopIteration):
            next(gen)

        with _state_lock:
            assert _state["warnings"] == [{"sentinel": "original"}]
            _state["warnings"] = []
