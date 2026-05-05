"""Tests for multi-architecture awareness (issue #24).

Covers:
- fetch_manifest_list: DockerHub and GHCR manifest list fetching + caching
- is_platform_supported: matching logic
- scanner.run_check: arch check integration (label, platform read, filtering)
"""

import pytest
from unittest.mock import MagicMock, patch, call

from app import http as http_mod
from app import config as config_mod
from app.registry.manifest import (
    fetch_manifest_list,
    is_platform_supported,
    clear_cache,
    _fetch_dockerhub_manifest_list,
    _fetch_ghcr_manifest_list,
)
from app.scanner import run_check


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_MULTI_ARCH_RESPONSE = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "linux", "architecture": "arm64"}},
        {"platform": {"os": "linux", "architecture": "arm", "variant": "v7"}},
    ],
}

_OCI_INDEX_RESPONSE = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.oci.image.index.v1+json",
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}},
        {"platform": {"os": "linux", "architecture": "arm64"}},
    ],
}

_SINGLE_ARCH_RESPONSE = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
    "layers": [],
}

_TOKEN_RESPONSE = {"token": "fake-registry-token"}


def _make_mock_resp(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


def _make_container(name, image_tag, labels=None, os="linux", arch="amd64"):
    c = MagicMock()
    c.name = name
    c.labels = labels or {}
    c.image.tags = [image_tag]
    c.image.attrs = {"Os": os, "Architecture": arch}
    c.attrs = {"Config": {"Image": image_tag}}
    return c


@pytest.fixture(autouse=True)
def _clear_manifest_cache():
    """Clear the manifest cache before every test."""
    clear_cache()
    yield
    clear_cache()


# ---------------------------------------------------------------------------
# is_platform_supported
# ---------------------------------------------------------------------------

class TestIsPlatformSupported:
    def test_none_platforms_means_compatible(self):
        assert is_platform_supported(None, "linux", "amd64") is True

    def test_matching_platform_returns_true(self):
        platforms = [{"os": "linux", "architecture": "arm64"}]
        assert is_platform_supported(platforms, "linux", "arm64") is True

    def test_non_matching_platform_returns_false(self):
        platforms = [{"os": "linux", "architecture": "amd64"}]
        assert is_platform_supported(platforms, "linux", "arm64") is False

    def test_matches_one_of_many(self):
        platforms = [
            {"os": "linux", "architecture": "amd64"},
            {"os": "linux", "architecture": "arm64"},
        ]
        assert is_platform_supported(platforms, "linux", "arm64") is True

    def test_empty_list_means_not_supported(self):
        assert is_platform_supported([], "linux", "amd64") is False

    def test_variant_ignored_in_matching(self):
        """Matching is on os + architecture only, variant is ignored."""
        platforms = [{"os": "linux", "architecture": "arm", "variant": "v7"}]
        assert is_platform_supported(platforms, "linux", "arm") is True


# ---------------------------------------------------------------------------
# fetch_manifest_list — caching
# ---------------------------------------------------------------------------

class TestManifestCache:
    @patch.object(http_mod, "http_session")
    def test_result_is_cached_on_second_call(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),          # auth token
            _make_mock_resp(_MULTI_ARCH_RESPONSE),     # manifest
        ]

        result1 = fetch_manifest_list("nginx", "1.25.0", "user", "pass", "")
        result2 = fetch_manifest_list("nginx", "1.25.0", "user", "pass", "")

        assert result1 == result2
        # Only 2 HTTP calls total (auth + manifest), not 4
        assert mock_session.get.call_count == 2

    @patch.object(http_mod, "http_session")
    def test_different_tags_not_shared_in_cache(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_MULTI_ARCH_RESPONSE),
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_SINGLE_ARCH_RESPONSE),
        ]

        r1 = fetch_manifest_list("nginx", "1.25.0", "", "", "")
        r2 = fetch_manifest_list("nginx", "1.26.0", "", "", "")

        assert r1 is not None   # multi-arch
        assert r2 is None       # single-arch


# ---------------------------------------------------------------------------
# DockerHub manifest list fetching
# ---------------------------------------------------------------------------

class TestFetchDockerhubManifestList:
    @patch.object(http_mod, "http_session")
    def test_multi_arch_image_returns_platforms(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_MULTI_ARCH_RESPONSE),
        ]
        result = _fetch_dockerhub_manifest_list("nginx", "1.25.0", "", "")
        assert result is not None
        assert {"os": "linux", "architecture": "amd64"} in result
        assert {"os": "linux", "architecture": "arm64"} in result

    @patch.object(http_mod, "http_session")
    def test_single_arch_image_returns_none(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_SINGLE_ARCH_RESPONSE),
        ]
        result = _fetch_dockerhub_manifest_list("nginx", "1.25.0", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_library_prefix_added_for_unnamespaced_image(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_SINGLE_ARCH_RESPONSE),
        ]
        _fetch_dockerhub_manifest_list("nginx", "latest", "", "")
        # The auth call should use library/nginx as scope
        auth_url = mock_session.get.call_args_list[0][0][0]
        assert "library%2Fnginx" in auth_url or "library/nginx" in str(mock_session.get.call_args_list[0])

    @patch.object(http_mod, "http_session")
    def test_token_failure_returns_none(self, mock_session):
        mock_session.get.side_effect = Exception("auth server down")
        result = _fetch_dockerhub_manifest_list("nginx", "latest", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_manifest_http_error_returns_none(self, mock_session):
        import requests
        token_resp = _make_mock_resp(_TOKEN_RESPONSE)
        error_resp = MagicMock()
        error_resp.raise_for_status.side_effect = requests.HTTPError("404")
        mock_session.get.side_effect = [token_resp, error_resp]
        result = _fetch_dockerhub_manifest_list("nginx", "latest", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_oci_index_response_returns_platforms(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_OCI_INDEX_RESPONSE),
        ]
        result = _fetch_dockerhub_manifest_list("nginx", "latest", "", "")
        assert result is not None
        assert {"os": "linux", "architecture": "amd64"} in result


# ---------------------------------------------------------------------------
# GHCR manifest list fetching
# ---------------------------------------------------------------------------

class TestFetchGhcrManifestList:
    @patch.object(http_mod, "http_session")
    def test_no_github_token_returns_none(self, mock_session):
        result = _fetch_ghcr_manifest_list("ghcr.io/owner/repo", "v1.0", "")
        assert result is None
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_multi_arch_image_returns_platforms(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_MULTI_ARCH_RESPONSE),
        ]
        result = _fetch_ghcr_manifest_list("ghcr.io/owner/repo", "v1.0", "gh-token")
        assert result is not None
        assert {"os": "linux", "architecture": "arm64"} in result

    @patch.object(http_mod, "http_session")
    def test_single_arch_image_returns_none(self, mock_session):
        mock_session.get.side_effect = [
            _make_mock_resp(_TOKEN_RESPONSE),
            _make_mock_resp(_SINGLE_ARCH_RESPONSE),
        ]
        result = _fetch_ghcr_manifest_list("ghcr.io/owner/repo", "v1.0", "gh-token")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_token_failure_returns_none(self, mock_session):
        mock_session.get.side_effect = Exception("ghcr.io down")
        result = _fetch_ghcr_manifest_list("ghcr.io/owner/repo", "v1.0", "gh-token")
        assert result is None


# ---------------------------------------------------------------------------
# scanner.run_check — arch check integration
# ---------------------------------------------------------------------------

class TestRunCheckArchCheck:
    """Integration tests for the arch check inside run_check()."""

    def _mock_docker(self, mock_docker_mod, containers):
        mock_client = MagicMock()
        mock_docker_mod.from_env.return_value = mock_client
        mock_client.containers.list.return_value = containers

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_unsupported_arch_update_is_skipped(
        self, mock_docker, _token, _tags, mock_manifest
    ):
        """Updates for amd64-only images are skipped on an arm64 host."""
        container = _make_container(
            "myapp", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
            os="linux", arch="arm64",
        )
        self._mock_docker(mock_docker, [container])
        # new tag only supports amd64
        mock_manifest.return_value = [{"os": "linux", "architecture": "amd64"}]

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_notify.assert_called_once()
        updates = mock_notify.call_args[0][0]
        assert updates == []  # no actionable updates

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_supported_arch_update_is_reported(
        self, mock_docker, _token, _tags, mock_manifest
    ):
        """Updates for multi-arch images are reported on arm64."""
        container = _make_container(
            "myapp", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
            os="linux", arch="arm64",
        )
        self._mock_docker(mock_docker, [container])
        # new tag supports arm64
        mock_manifest.return_value = [
            {"os": "linux", "architecture": "amd64"},
            {"os": "linux", "architecture": "arm64"},
        ]

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_notify.assert_called_once()
        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1
        assert updates[0].new_version == "2.0.0"

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_single_arch_image_treated_as_compatible(
        self, mock_docker, _token, _tags, mock_manifest
    ):
        """Single-arch images (manifest list returns None) are treated as compatible."""
        container = _make_container(
            "myapp", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
            os="linux", arch="arm64",
        )
        self._mock_docker(mock_docker, [container])
        mock_manifest.return_value = None  # single-arch → no manifest list

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1
        assert updates[0].new_version == "2.0.0"

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_check_arch_false_label_disables_check(
        self, mock_docker, _token, _tags, mock_manifest
    ):
        """check-arch=false disables the arch check for that container."""
        container = _make_container(
            "myapp", "nginx:1.0.0",
            {
                "docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$",
                "docker-update-monitor.check-arch": "false",
            },
            os="linux", arch="arm64",
        )
        self._mock_docker(mock_docker, [container])
        mock_manifest.return_value = [{"os": "linux", "architecture": "amd64"}]

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # fetch_manifest_list must NOT have been called
        mock_manifest.assert_not_called()
        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_missing_platform_info_skips_arch_check(
        self, mock_docker, _token, _tags, mock_manifest, caplog
    ):
        """Container with no Os/Architecture in image attrs skips the arch check."""
        import logging

        container = _make_container(
            "myapp", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        container.image.attrs = {}  # no Os/Architecture

        self._mock_docker(mock_docker, [container])

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.WARNING):
            run_check()

        assert "Platform info unavailable" in caplog.text
        mock_manifest.assert_not_called()
        # update should still be reported
        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_image_attrs_exception_skips_arch_check(
        self, mock_docker, _token, _tags, mock_manifest, caplog
    ):
        """Exception reading image.attrs falls back gracefully."""
        import logging

        container = _make_container(
            "myapp", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        type(container.image).attrs = property(lambda self: (_ for _ in ()).throw(RuntimeError("boom")))

        self._mock_docker(mock_docker, [container])

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.WARNING):
            run_check()

        assert "Could not read platform info" in caplog.text
        mock_manifest.assert_not_called()
        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1

    @patch("app.scanner.fetch_manifest_list")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_arch_check_default_is_enabled(
        self, mock_docker, _token, _tags, mock_manifest
    ):
        """check-arch defaults to enabled when label is absent."""
        container = _make_container(
            "myapp", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
            os="linux", arch="amd64",
        )
        self._mock_docker(mock_docker, [container])
        mock_manifest.return_value = [{"os": "linux", "architecture": "amd64"}]

        with patch("app.scanner.notify"), patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_manifest.assert_called_once()
