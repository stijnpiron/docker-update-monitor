"""Integration test: start the dashboard server and verify it renders."""

import threading
import time
import urllib.request
import urllib.error
from unittest.mock import patch

import pytest

from app.dashboard import create_app


@pytest.fixture
def live_server():
    """Start the Flask app on a random port and yield the base URL."""
    app = create_app()
    app.config["TESTING"] = True

    # Use port 0 to get a random available port
    from werkzeug.serving import make_server

    server = make_server("127.0.0.1", 0, app)
    port = server.socket.getsockname()[1]
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    yield f"http://127.0.0.1:{port}"

    server.shutdown()


class TestDashboardIntegration:
    """Integration tests that start the actual server."""

    @patch("app.dashboard.get_all_updates")
    def test_dashboard_renders_over_http(self, mock_updates, live_server):
        mock_updates.return_value = [
            {
                "container_name": "test-app",
                "service_name": "app",
                "stack": "prod",
                "image": "nginx",
                "current_version": "1.24.0",
                "new_version": "1.25.0",
                "update_type": "minor",
                "status": "new",
                "first_seen_at": "2026-04-30T10:00:00",
            }
        ]

        resp = urllib.request.urlopen(f"{live_server}/")
        assert resp.status == 200
        html = resp.read().decode()
        assert "Docker Update Monitor" in html
        assert "test-app" in html
        assert "<table" in html

    @patch("app.dashboard.get_all_updates")
    def test_api_updates_over_http(self, mock_updates, live_server):
        mock_updates.return_value = [
            {"container_name": "x", "status": "known", "image": "redis"}
        ]

        resp = urllib.request.urlopen(f"{live_server}/api/updates")
        assert resp.status == 200
        import json
        data = json.loads(resp.read())
        assert isinstance(data, list)
        assert data[0]["container_name"] == "x"

    def test_api_scan_over_http(self, live_server):
        req = urllib.request.Request(f"{live_server}/api/scan", method="POST")
        resp = urllib.request.urlopen(req)
        assert resp.status == 202

    @patch("app.dashboard._build_response")
    def test_health_endpoint_over_http(self, mock_build, live_server):
        mock_build.return_value = (200, {"status": "ok"})
        resp = urllib.request.urlopen(f"{live_server}/health")
        assert resp.status == 200
