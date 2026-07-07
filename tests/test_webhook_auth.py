"""Tests for webhook authentication support (NOTIFY_AUTH_TYPE / NOTIFY_AUTH_TOKEN)."""

import base64
from unittest.mock import MagicMock, patch

from app import config as config_mod
from app import http as http_mod
from app.models import UpdateInfo
from app.notifications.webhook import notify


def _make_update():
    return UpdateInfo(
        container_name="test",
        service_name="test",
        stack="stack",
        image="nginx",
        current_version="1.0.0",
        new_version="1.1.0",
        update_type="minor",
        status="new",
    )


class TestWebhookAuthBearer:
    """Bearer token authentication."""

    @patch.object(http_mod, "http_session")
    def test_bearer_auth_sends_authorization_header(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "bearer"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", "my-secret-token"):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == "Bearer my-secret-token"

    @patch.object(http_mod, "http_session")
    def test_bearer_auth_case_insensitive(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "bearer"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", "xyz"):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == "Bearer xyz"


class TestWebhookAuthBasic:
    """Basic authentication — token is supplied as `user:pass` and base64-encoded by the app."""

    @patch.object(http_mod, "http_session")
    def test_basic_auth_base64_encodes_user_pass(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "basic"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", "user:pass"):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == "Basic dXNlcjpwYXNz"

    @patch.object(http_mod, "http_session")
    def test_basic_auth_encodes_special_characters(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        token = "admin:p@ss w0rd!"
        expected = base64.b64encode(token.encode("utf-8")).decode("ascii")

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "basic"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", token):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == f"Basic {expected}"

    @patch.object(http_mod, "http_session")
    def test_basic_auth_encodes_utf8(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        token = "café:naïve"
        expected = base64.b64encode(token.encode("utf-8")).decode("ascii")

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "basic"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", token):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert headers["Authorization"] == f"Basic {expected}"


class TestWebhookAuthNone:
    """No authentication (backward compatible)."""

    @patch.object(http_mod, "http_session")
    def test_no_auth_type_sends_no_authorization_header(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", ""), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "Authorization" not in headers

    @patch.object(http_mod, "http_session")
    def test_auth_type_without_token_sends_no_authorization_header(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "bearer"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", ""):
            notify([_make_update()])

        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "Authorization" not in headers


class TestWebhookAuthInvalid:
    """Invalid auth type logs warning but still sends."""

    @patch.object(http_mod, "http_session")
    def test_invalid_auth_type_logs_warning_and_sends(self, mock_session, caplog):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "oauth2"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", "some-token"):
            notify([_make_update()])

        # Should still send the request
        mock_session.post.assert_called_once()
        # Should not include Authorization header
        call_kwargs = mock_session.post.call_args
        headers = call_kwargs.kwargs.get("headers") or call_kwargs[1].get("headers")
        assert "Authorization" not in headers
        # Should log a warning
        assert "Unknown NOTIFY_AUTH_TYPE" in caplog.text


class TestTokenNotLogged:
    """Token value must not appear in log output."""

    @patch.object(http_mod, "http_session")
    def test_token_not_in_logs(self, mock_session, caplog):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        secret = "super-secret-token-value-12345"

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False), \
             patch.object(config_mod, "NOTIFY_AUTH_TYPE", "bearer"), \
             patch.object(config_mod, "NOTIFY_AUTH_TOKEN", secret):
            notify([_make_update()])

        assert secret not in caplog.text
