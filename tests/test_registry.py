"""Unit tests for registry helper functions (DockerHub, GHCR, fetch_all_tags)."""

from unittest.mock import MagicMock, patch
import requests

from app import http as http_mod
from app.registry import fetch_all_tags
from app.registry.dockerhub import get_dockerhub_token, _fetch_dockerhub_tags
from app.registry.ghcr import _get_ghcr_token, _fetch_ghcr_tags


class TestGetDockerhubToken:
    """Tests for get_dockerhub_token()."""

    @patch.object(http_mod, "http_session")
    def test_no_credentials_returns_none(self, mock_session):
        assert get_dockerhub_token("", "") is None
        mock_session.post.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_no_username_returns_none(self, mock_session):
        assert get_dockerhub_token("", "password") is None

    @patch.object(http_mod, "http_session")
    def test_no_password_returns_none(self, mock_session):
        assert get_dockerhub_token("user", "") is None

    @patch.object(http_mod, "http_session")
    def test_auth_failure_returns_none(self, mock_session):
        mock_session.post.side_effect = Exception("401 Unauthorized")
        result = get_dockerhub_token("user", "badpass")
        assert result is None


class TestFetchDockerhubTags:
    """Tests for _fetch_dockerhub_tags()."""

    @patch.object(http_mod, "http_session")
    def test_single_page_returns_tags(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"name": "1.0.0"}, {"name": "1.1.0"}],
            "next": None,
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        tags = _fetch_dockerhub_tags("nginx", "token")
        assert tags == ["1.0.0", "1.1.0"]

    @patch.object(http_mod, "http_session")
    def test_early_stop_on_current_tag(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {
            "results": [{"name": "2.0.0"}, {"name": "1.0.0"}],
            "next": "http://next-page",
        }
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        tags = _fetch_dockerhub_tags("nginx", "token", current_tag="1.0.0")
        # Should stop after first page since current_tag was found
        assert "1.0.0" in tags
        assert mock_session.get.call_count == 1

    @patch.object(http_mod, "http_session")
    def test_strips_docker_io_prefix(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"results": [{"name": "latest"}], "next": None}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        _fetch_dockerhub_tags("docker.io/library/nginx", "token")
        url = mock_session.get.call_args[0][0]
        assert "library" in url and "nginx" in url

    @patch.object(http_mod, "http_session")
    def test_http_error_returns_partial(self, mock_session):
        mock_session.get.side_effect = requests.HTTPError("500 Server Error")
        tags = _fetch_dockerhub_tags("nginx", "token")
        assert tags == []

    @patch.object(http_mod, "http_session")
    def test_general_exception_returns_empty(self, mock_session):
        mock_session.get.side_effect = Exception("network timeout")
        tags = _fetch_dockerhub_tags("nginx", "token")
        assert tags == []


class TestGetGhcrToken:
    """Tests for _get_ghcr_token()."""

    @patch.object(http_mod, "http_session")
    def test_successful_token_exchange(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"token": "ghcr-token-abc"}
        mock_resp.raise_for_status = MagicMock()
        mock_session.get.return_value = mock_resp

        result = _get_ghcr_token("owner", "repo", "gh_pat_xxx")
        assert result == "ghcr-token-abc"

    @patch.object(http_mod, "http_session")
    def test_failed_token_exchange_returns_none(self, mock_session):
        mock_session.get.side_effect = Exception("403 Forbidden")
        result = _get_ghcr_token("owner", "repo", "gh_pat_xxx")
        assert result is None


class TestFetchGhcrTags:
    """Tests for _fetch_ghcr_tags()."""

    @patch.object(http_mod, "http_session")
    def test_no_github_token_returns_empty(self, mock_session):
        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "")
        assert tags == []
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_invalid_image_path_returns_empty(self, mock_session):
        tags = _fetch_ghcr_tags("ghcr.io/invalid", "gh_token")
        assert tags == []

    @patch("app.registry.ghcr._get_ghcr_token", return_value=None)
    @patch.object(http_mod, "http_session")
    def test_no_pull_token_returns_empty(self, mock_session, mock_get_token):
        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == []

    @patch("app.registry.ghcr._get_ghcr_token", return_value="pull-token")
    @patch.object(http_mod, "http_session")
    def test_successful_fetch(self, mock_session, mock_get_token):
        mock_resp = MagicMock()
        mock_resp.json.return_value = {"tags": ["v1.0.0", "v1.1.0"]}
        mock_resp.raise_for_status = MagicMock()
        mock_resp.headers = {"Link": ""}
        mock_session.get.return_value = mock_resp

        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == ["v1.0.0", "v1.1.0"]

    @patch("app.registry.ghcr._get_ghcr_token", return_value="pull-token")
    @patch.object(http_mod, "http_session")
    def test_pagination_with_link_header(self, mock_session, mock_get_token):
        resp1 = MagicMock()
        resp1.json.return_value = {"tags": ["v1.0.0"]}
        resp1.raise_for_status = MagicMock()
        resp1.headers = {"Link": '<https://ghcr.io/v2/owner/repo/tags/list?n=100&last=v1.0.0>; rel="next"'}

        resp2 = MagicMock()
        resp2.json.return_value = {"tags": ["v2.0.0"]}
        resp2.raise_for_status = MagicMock()
        resp2.headers = {"Link": ""}

        mock_session.get.side_effect = [resp1, resp2]

        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == ["v1.0.0", "v2.0.0"]
        assert mock_session.get.call_count == 2

    @patch("app.registry.ghcr._get_ghcr_token", return_value="pull-token")
    @patch.object(http_mod, "http_session")
    def test_http_error_returns_partial(self, mock_session, mock_get_token):
        mock_session.get.side_effect = requests.HTTPError("403 Forbidden")
        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == []

    @patch("app.registry.ghcr._get_ghcr_token", return_value="pull-token")
    @patch.object(http_mod, "http_session")
    def test_general_exception_returns_empty(self, mock_session, mock_get_token):
        mock_session.get.side_effect = Exception("connection reset")
        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == []


class TestFetchAllTags:
    """Tests for fetch_all_tags() routing."""

    @patch("app.registry._fetch_dockerhub_tags", return_value=["1.0.0"])
    def test_routes_dockerhub(self, mock_fetch):
        tags = fetch_all_tags("nginx", "token", "")
        assert tags == ["1.0.0"]
        mock_fetch.assert_called_once_with("nginx", "token", None)

    @patch("app.registry._fetch_ghcr_tags", return_value=["v1.0.0"])
    def test_routes_ghcr(self, mock_fetch):
        tags = fetch_all_tags("ghcr.io/owner/repo", None, "gh_token")
        assert tags == ["v1.0.0"]
        mock_fetch.assert_called_once_with("ghcr.io/owner/repo", "gh_token")

    def test_unknown_registry_returns_empty(self):
        tags = fetch_all_tags("quay.io/org/image", None, "")
        assert tags == []
