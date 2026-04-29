"""Unit tests for notify() — DRY_RUN, no endpoint, successful POST, failed POST."""

from unittest.mock import MagicMock, patch

import pytest

from app import config as config_mod
from app import http as http_mod
from app.models import UpdateInfo
from app.notifications.webhook import notify


def _make_update(**kwargs):
    defaults = dict(
        container_name="test-app",
        stack="mystack",
        image="nginx",
        current_version="1.0.0",
        new_version="1.1.0",
        update_type="minor",
        status="new",
    )
    defaults.update(kwargs)
    return UpdateInfo(**defaults)


class TestNotifyDryRun:
    """DRY_RUN=true: no HTTP POST is made."""

    @patch.object(http_mod, "http_session")
    def test_dry_run_does_not_post(self, mock_session):
        with patch.object(config_mod, "DRY_RUN", True), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"):
            notify([_make_update()])

        mock_session.post.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_dry_run_logs_payload(self, mock_session, caplog):
        import logging

        with patch.object(config_mod, "DRY_RUN", True), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             caplog.at_level(logging.INFO):
            notify([_make_update()])

        assert "DRY_RUN" in caplog.text
        assert "nginx" in caplog.text


class TestNotifyNoEndpoint:
    """No NOTIFY_ENDPOINT set: logs updates but does not POST."""

    @patch.object(http_mod, "http_session")
    def test_no_endpoint_does_not_post(self, mock_session):
        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", ""):
            notify([_make_update()])

        mock_session.post.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_no_endpoint_logs_warning(self, mock_session, caplog):
        import logging

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", ""), \
             caplog.at_level(logging.WARNING):
            notify([_make_update()])

        assert "No NOTIFY_ENDPOINT" in caplog.text


class TestNotifySuccessfulPost:
    """Successful POST to endpoint."""

    @patch.object(http_mod, "http_session")
    def test_successful_post(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update()])

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        payload = call_kwargs[1]["json"]
        assert "new" in payload
        assert payload["new"][0]["container_name"] == "test-app"
        assert payload["new"][0]["new_version"] == "1.1.0"

    @patch.object(http_mod, "http_session")
    def test_successful_post_logs_count(self, mock_session, caplog):
        import logging

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""), \
             caplog.at_level(logging.INFO):
            notify([_make_update()])

        assert "1 update(s)" in caplog.text


class TestNotifyFailedPost:
    """Failed POST logs error but does not raise."""

    @patch.object(http_mod, "http_session")
    def test_failed_post_logs_error(self, mock_session, caplog):
        import logging

        mock_session.post.side_effect = Exception("Connection refused")

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""), \
             caplog.at_level(logging.ERROR):
            notify([_make_update()])

        assert "Failed to notify" in caplog.text
        assert "Connection refused" in caplog.text

    @patch.object(http_mod, "http_session")
    def test_failed_post_does_not_raise(self, mock_session):
        mock_session.post.side_effect = Exception("timeout")

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            # Should not raise
            notify([_make_update()])


class TestNotifyEmptyList:
    """Empty update list: no action taken."""

    @patch.object(http_mod, "http_session")
    def test_empty_updates_returns_immediately(self, mock_session):
        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"):
            notify([])

        mock_session.post.assert_not_called()
