"""Unit tests for registry helper functions (DockerHub, GHCR, fetch_all_tags)."""

from unittest.mock import MagicMock, patch
import requests

from app import http as http_mod
from app.registry import fetch_all_tags
from app.registry.dockerhub import get_dockerhub_token, _fetch_dockerhub_tags
from app.registry.ghcr import _fetch_ghcr_tags


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

    @patch.object(http_mod, "http_session")
    def test_successful_fetch(self, mock_session):
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = [
            {"metadata": {"container": {"tags": ["v1.0.0", "v1.1.0"]}}},
            {"metadata": {"container": {"tags": ["v0.9.0"]}}},
        ]
        resp1.raise_for_status = MagicMock()

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = []
        resp2.raise_for_status = MagicMock()

        mock_session.get.side_effect = [resp1, resp2]

        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == ["v1.0.0", "v1.1.0", "v0.9.0"]

    @patch.object(http_mod, "http_session")
    def test_early_stop_on_current_tag(self, mock_session):
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = [
            {"metadata": {"container": {"tags": ["v2.0.0"]}}},
            {"metadata": {"container": {"tags": ["v1.5.0"]}}},
        ]
        resp1.raise_for_status = MagicMock()

        # Should not reach page 2 because current_tag is found on page 1
        mock_session.get.return_value = resp1

        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token", current_tag="v1.5.0")
        assert "v2.0.0" in tags
        assert "v1.5.0" in tags
        assert mock_session.get.call_count == 1

    @patch.object(http_mod, "http_session")
    def test_pagination_continues_until_current_tag(self, mock_session):
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = [
            {"metadata": {"container": {"tags": ["v2.0.0"]}}},
        ]
        resp1.raise_for_status = MagicMock()

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = [
            {"metadata": {"container": {"tags": ["v1.0.0"]}}},
        ]
        resp2.raise_for_status = MagicMock()

        mock_session.get.side_effect = [resp1, resp2]

        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token", current_tag="v1.0.0")
        assert tags == ["v2.0.0", "v1.0.0"]
        assert mock_session.get.call_count == 2

    @patch.object(http_mod, "http_session")
    def test_fallback_to_user_endpoint_on_404(self, mock_session):
        resp_404 = MagicMock()
        resp_404.status_code = 404

        resp_ok = MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = [
            {"metadata": {"container": {"tags": ["v1.0.0"]}}},
        ]
        resp_ok.raise_for_status = MagicMock()

        resp_empty = MagicMock()
        resp_empty.status_code = 200
        resp_empty.json.return_value = []
        resp_empty.raise_for_status = MagicMock()

        mock_session.get.side_effect = [resp_404, resp_ok, resp_empty]

        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == ["v1.0.0"]
        # First call to org endpoint (404), second to user endpoint (ok), third empty page
        assert mock_session.get.call_count == 3

    @patch.object(http_mod, "http_session")
    def test_http_error_returns_partial(self, mock_session):
        mock_session.get.side_effect = requests.HTTPError("403 Forbidden")
        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == []

    @patch.object(http_mod, "http_session")
    def test_general_exception_returns_empty(self, mock_session):
        mock_session.get.side_effect = Exception("connection reset")
        tags = _fetch_ghcr_tags("ghcr.io/owner/repo", "gh_token")
        assert tags == []

    @patch.object(http_mod, "http_session")
    def test_lscr_io_routes_through_ghcr(self, mock_session):
        resp1 = MagicMock()
        resp1.status_code = 200
        resp1.json.return_value = [
            {"metadata": {"container": {"tags": ["1.5.4", "1.5.3"]}}},
        ]
        resp1.raise_for_status = MagicMock()

        resp2 = MagicMock()
        resp2.status_code = 200
        resp2.json.return_value = []
        resp2.raise_for_status = MagicMock()

        mock_session.get.side_effect = [resp1, resp2]

        tags = _fetch_ghcr_tags("lscr.io/linuxserver/bazarr", "gh_token")
        assert tags == ["1.5.4", "1.5.3"]
        # Verify the GitHub API URL uses the correct owner/repo
        url = mock_session.get.call_args_list[0][0][0]
        assert "linuxserver" in url
        assert "bazarr" in url


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
        mock_fetch.assert_called_once_with("ghcr.io/owner/repo", "gh_token", None)

    @patch("app.registry._fetch_ghcr_tags", return_value=["1.5.4"])
    def test_routes_lscr_io_through_ghcr(self, mock_fetch):
        tags = fetch_all_tags("lscr.io/linuxserver/bazarr", None, "gh_token")
        assert tags == ["1.5.4"]
        mock_fetch.assert_called_once_with("lscr.io/linuxserver/bazarr", "gh_token", None)

    def test_unknown_registry_returns_empty(self):
        tags = fetch_all_tags("quay.io/org/image", None, "")
        assert tags == []
