"""Unit tests for the Flask dashboard routes."""

import json
from unittest.mock import patch, MagicMock

import pytest

from app.dashboard import create_app, _scan_trigger


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
    def test_returns_503_before_first_check(self, mock_build, client):
        mock_build.return_value = (503, {"status": "unavailable", "reason": "no check completed yet"})
        resp = client.get("/health")
        assert resp.status_code == 503
        data = json.loads(resp.data)
        assert data["status"] == "unavailable"
