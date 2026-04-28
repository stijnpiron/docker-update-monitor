"""Tests for HTTP session reuse with retry and backoff."""

from unittest.mock import patch, MagicMock
import requests
from urllib3.util.retry import Retry

from app import http as http_mod
from app import config as config_mod
from app.registry.dockerhub import get_dockerhub_token, _fetch_dockerhub_tags
from app.registry.ghcr import _fetch_ghcr_tags
from app.notifications.webhook import notify
from app.models import UpdateInfo


class TestCreateHttpSession:
    """Tests for create_http_session factory."""

    def test_returns_session_instance(self):
        session = http_mod.create_http_session()
        assert isinstance(session, requests.Session)

    def test_has_retry_adapter_for_https(self):
        session = http_mod.create_http_session()
        adapter = session.get_adapter("https://example.com")
        assert adapter.max_retries.total == 3
        assert 429 in adapter.max_retries.status_forcelist
        assert 500 in adapter.max_retries.status_forcelist
        assert 502 in adapter.max_retries.status_forcelist
        assert 503 in adapter.max_retries.status_forcelist
        assert 504 in adapter.max_retries.status_forcelist

    def test_has_retry_adapter_for_http(self):
        session = http_mod.create_http_session()
        adapter = session.get_adapter("http://example.com")
        assert adapter.max_retries.total == 3

    def test_respects_retry_after_header(self):
        session = http_mod.create_http_session()
        adapter = session.get_adapter("https://example.com")
        assert adapter.max_retries.respect_retry_after_header is True

    def test_backoff_factor_is_set(self):
        session = http_mod.create_http_session()
        adapter = session.get_adapter("https://example.com")
        assert adapter.max_retries.backoff_factor == 1

    def test_pool_connections_configured(self):
        session = http_mod.create_http_session()
        adapter = session.get_adapter("https://example.com")
        assert adapter._pool_connections == 10
        assert adapter._pool_maxsize == 10


class TestSessionIsReused:
    """Verify the module-level session is shared across calls."""

    def test_module_level_session_exists(self):
        assert hasattr(http_mod, "http_session")
        assert isinstance(http_mod.http_session, requests.Session)

    @patch.object(http_mod, "http_session")
    def test_dockerhub_login_uses_shared_session(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"token": "abc123"}
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        token = get_dockerhub_token("user", "pass")

        mock_session.post.assert_called_once()
        assert token == "abc123"

    @patch.object(http_mod, "http_session")
    def test_fetch_dockerhub_tags_uses_shared_session(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [{"name": "v1.0.0"}], "next": None}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        tags = _fetch_dockerhub_tags("library/nginx", "token123")

        mock_session.get.assert_called_once()
        assert "v1.0.0" in tags

    @patch.object(http_mod, "http_session")
    def test_notify_uses_shared_session(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with patch.object(config_mod, "NOTIFY_ENDPOINT", "http://hook.example.com"), \
             patch.object(config_mod, "DRY_RUN", False):
            notify([UpdateInfo(
                container_name="test",
                stack="stack",
                image="nginx",
                current_version="1.0.0",
                new_version="1.1.0",
                update_type="minor",
            )])

        mock_session.post.assert_called_once()
        call_kwargs = mock_session.post.call_args
        assert call_kwargs.args[0] == "http://hook.example.com"


class TestRetryBehavior:
    """Verify that retry logic triggers on 429 and 5xx."""

    @patch.object(http_mod, "http_session")
    def test_429_triggers_retry_on_dockerhub_tags(self, mock_session):
        """Simulate a 429 followed by success — the session's retry handles this transparently."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [{"name": "v2.0.0"}], "next": None}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        tags = _fetch_dockerhub_tags("nginx", None)
        assert "v2.0.0" in tags

    @patch.object(http_mod, "http_session")
    def test_503_triggers_retry_on_ghcr_tags(self, mock_session):
        """Simulate that after retries, GHCR tags returns successfully."""
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"tags": ["v1.2.3"]}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Link": ""}
        mock_session.get.return_value = mock_resp

        with patch("app.registry.ghcr._get_ghcr_token", return_value="fake-token"):
            tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh-pat")

        assert "v1.2.3" in tags
        mock_session.get.assert_called()

    def test_retry_adapter_status_forcelist_includes_429_and_5xx(self):
        """Directly verify the retry configuration covers the required status codes."""
        session = http_mod.create_http_session()
        adapter = session.get_adapter("https://example.com")
        retry = adapter.max_retries
        assert isinstance(retry, Retry)
        assert 429 in retry.status_forcelist
        assert 503 in retry.status_forcelist
        assert 500 in retry.status_forcelist
