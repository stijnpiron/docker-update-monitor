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
        service_name="app",
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


class TestNotifyPayloadStructure:
    """Payload grouping, field removal, mismatches and warnings."""

    @patch.object(http_mod, "http_session")
    def test_payload_groups_by_status(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        updates = [
            _make_update(container_name="a", status="new"),
            _make_update(container_name="b", status="known"),
            _make_update(container_name="c", status="resolved"),
        ]

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify(updates)

        payload = mock_session.post.call_args[1]["json"]
        assert "new" in payload
        assert "known" in payload
        assert "resolved" in payload
        assert payload["new"][0]["container_name"] == "a"
        assert payload["known"][0]["container_name"] == "b"
        assert payload["resolved"][0]["container_name"] == "c"

    @patch.object(http_mod, "http_session")
    def test_payload_removes_status_field_from_entries(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update()])

        payload = mock_session.post.call_args[1]["json"]
        for entry in payload["new"]:
            assert "status" not in entry

    @patch.object(http_mod, "http_session")
    def test_payload_includes_mismatches(self, mock_session):
        from app.models import RegexMismatch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        mismatch = RegexMismatch(
            container_name="app", service_name="app", stack="stack",
            image="nginx", current_tag="latest", pattern=r"^\d+$",
            reason="did not match",
        )

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update()], mismatches=[mismatch])

        payload = mock_session.post.call_args[1]["json"]
        assert "regex_mismatches" in payload
        assert payload["regex_mismatches"][0]["container_name"] == "app"

    @patch.object(http_mod, "http_session")
    def test_payload_includes_warnings(self, mock_session):
        from app.models import ScanWarning

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        warning = ScanWarning(
            container_name="app", image="nginx",
            level="warning", message="fetch failed",
        )

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update()], warnings=[warning])

        payload = mock_session.post.call_args[1]["json"]
        assert "warnings" in payload
        assert payload["warnings"][0]["message"] == "fetch failed"

    @patch.object(http_mod, "http_session")
    def test_payload_omits_empty_status_groups(self, mock_session):
        """If no 'known' updates, that key is absent from payload."""
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update(status="new")])

        payload = mock_session.post.call_args[1]["json"]
        assert "new" in payload
        assert "known" not in payload
        assert "resolved" not in payload


class TestNotifyHttpErrors:
    """HTTP errors and timeouts."""

    @patch.object(http_mod, "http_session")
    def test_raise_for_status_error_is_caught(self, mock_session, caplog):
        import logging
        from requests.exceptions import HTTPError

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = HTTPError("500 Server Error")
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""), \
             caplog.at_level(logging.ERROR):
            notify([_make_update()])  # should not raise

        assert "Failed to notify" in caplog.text

    @patch.object(http_mod, "http_session")
    def test_timeout_error_is_caught(self, mock_session, caplog):
        import logging
        from requests.exceptions import Timeout

        mock_session.post.side_effect = Timeout("Read timed out")

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""), \
             caplog.at_level(logging.ERROR):
            notify([_make_update()])  # should not raise

        assert "Failed to notify" in caplog.text


class TestNotifyMisconfiguredAuth:
    """Edge cases in auth configuration."""

    @patch.object(http_mod, "http_session")
    def test_unknown_auth_type_logs_warning(self, mock_session, caplog):
        import logging

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "oauth2"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", "token"), \
             caplog.at_level(logging.WARNING):
            notify([_make_update()])

        assert "Unknown NOTIFY_AUTH_TYPE" in caplog.text
        # Should still POST without Authorization header
        headers = mock_session.post.call_args[1]["headers"]
        assert "Authorization" not in headers

    @patch.object(http_mod, "http_session")
    def test_mismatches_only_triggers_post(self, mock_session):
        """If only mismatches are present (no updates), webhook still fires."""
        from app.models import RegexMismatch

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        mismatch = RegexMismatch(
            container_name="app", service_name="app", stack="stack",
            image="nginx", current_tag="latest", pattern=r"^\d+$",
            reason="did not match",
        )

        with patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([], mismatches=[mismatch])

        mock_session.post.assert_called_once()
