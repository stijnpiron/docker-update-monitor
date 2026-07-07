"""Unit tests for app.registry.manifest — digest and platform-digest HTTP paths.

The manifest-list path is covered by test_arch_check.py. This module targets the
previously-untested code:
- fetch_digest + _fetch_dockerhub_digest / _fetch_ghcr_digest / _fetch_digest_from_url
- fetch_platform_digest + its DockerHub/GHCR helpers
- fetch_manifest_list registry dispatch (GHCR + unknown) and the schemaVersion-2
  fallback / generic-exception branches in _fetch_platforms_from_url

HTTP is mocked at app.http.http_session (the same seam test_arch_check.py uses):
- _get_token issues a GET to the token endpoint
- _fetch_digest_from_url issues a HEAD (returns the Docker-Content-Digest header)
- _fetch_platforms_from_url / _fetch_platform_digest_from_url issue a GET (parse JSON)
"""

import pytest
import requests
from unittest.mock import MagicMock, patch

from app import http as http_mod
from app.registry.manifest import (
    fetch_manifest_list,
    fetch_digest,
    fetch_platform_digest,
    clear_cache,
    clear_digest_cache,
    clear_platform_digest_cache,
    _fetch_platforms_from_url,
    _fetch_ghcr_manifest_list,
)


# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_TOKEN_RESPONSE = {"token": "fake-registry-token"}

# Manifest list carrying per-platform digests (what a fat manifest returns).
_MANIFEST_LIST_WITH_DIGESTS = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:amd64digest"},
        {"platform": {"os": "linux", "architecture": "arm64"}, "digest": "sha256:arm64digest"},
    ],
}

# OCI index that omits mediaType — exercises the schemaVersion==2 fallback branch.
_INDEX_WITHOUT_MEDIATYPE = {
    "schemaVersion": 2,
    "manifests": [
        {"platform": {"os": "linux", "architecture": "amd64"}, "digest": "sha256:amd64digest"},
    ],
}

# Single-arch (image) manifest — no manifests[] list.
_SINGLE_ARCH_RESPONSE = {
    "schemaVersion": 2,
    "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
    "layers": [],
}


def _make_get_resp(json_body, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = json_body
    resp.raise_for_status = MagicMock()
    return resp


def _make_head_resp(digest, status_code=200):
    resp = MagicMock()
    resp.status_code = status_code
    resp.headers = {"Docker-Content-Digest": digest} if digest else {}
    resp.raise_for_status = MagicMock()
    return resp


@pytest.fixture(autouse=True)
def _clear_all_caches():
    """Reset every module-level cache around each test."""
    clear_cache()
    clear_digest_cache()
    clear_platform_digest_cache()
    yield
    clear_cache()
    clear_digest_cache()
    clear_platform_digest_cache()


# ---------------------------------------------------------------------------
# _fetch_platforms_from_url — fallback + generic-error branches
# ---------------------------------------------------------------------------

class TestFetchPlatformsFromUrl:
    @patch.object(http_mod, "http_session")
    def test_schema_v2_manifests_without_mediatype(self, mock_session):
        """An index that omits mediaType but has manifests[] is treated as multi-arch."""
        mock_session.get.return_value = _make_get_resp(_INDEX_WITHOUT_MEDIATYPE)
        result = _fetch_platforms_from_url("https://reg/v2/x/manifests/tag", {})
        assert result == [{"os": "linux", "architecture": "amd64"}]

    @patch.object(http_mod, "http_session")
    def test_generic_exception_returns_none(self, mock_session):
        """A non-HTTP error during the GET is swallowed and yields None."""
        mock_session.get.side_effect = ValueError("connection reset")
        result = _fetch_platforms_from_url("https://reg/v2/x/manifests/tag", {})
        assert result is None


# ---------------------------------------------------------------------------
# fetch_manifest_list — registry dispatch
# ---------------------------------------------------------------------------

class TestFetchManifestListDispatch:
    @patch.object(http_mod, "http_session")
    def test_ghcr_image_dispatches_to_ghcr(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        result = fetch_manifest_list("ghcr.io/owner/repo", "v1.0", "", "", "gh-token")
        assert result is not None
        assert {"os": "linux", "architecture": "arm64"} in result

    @patch.object(http_mod, "http_session")
    def test_unknown_registry_returns_none(self, mock_session):
        result = fetch_manifest_list("myregistry.example.com/team/app", "v1.0", "", "", "")
        assert result is None
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_ghcr_full_url_with_scheme(self, mock_session):
        """A scheme-qualified GHCR ref resolves its host via urlparse().hostname."""
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        result = _fetch_ghcr_manifest_list("https://ghcr.io/owner/repo", "v1.0", "gh-token")
        assert result is not None
        # The registry token endpoint should target ghcr.io
        token_url = mock_session.get.call_args_list[0][0][0]
        assert "ghcr.io/token" in token_url


# ---------------------------------------------------------------------------
# fetch_digest
# ---------------------------------------------------------------------------

class TestFetchDigest:
    @patch.object(http_mod, "http_session")
    def test_dockerhub_success(self, mock_session):
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)  # token
        mock_session.head.return_value = _make_head_resp("sha256:abc123")
        result = fetch_digest("nginx", "1.25.0", "user", "pass", "")
        assert result == "sha256:abc123"
        # Unnamespaced image gets the library/ prefix in the token scope
        token_call = mock_session.get.call_args_list[0]
        assert token_call.kwargs["params"]["scope"] == "repository:library/nginx:pull"

    @patch.object(http_mod, "http_session")
    def test_dockerhub_token_failure_returns_none(self, mock_session):
        mock_session.get.return_value = _make_get_resp({})  # token endpoint yields nothing
        result = fetch_digest("nginx", "1.25.0", "", "", "")
        assert result is None
        mock_session.head.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_dockerhub_head_error_returns_none(self, mock_session):
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)
        mock_session.head.side_effect = requests.HTTPError("404")
        result = fetch_digest("nginx", "1.25.0", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_dockerhub_missing_digest_header_returns_none(self, mock_session):
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)
        mock_session.head.return_value = _make_head_resp(None)  # no Docker-Content-Digest
        result = fetch_digest("nginx", "1.25.0", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_ghcr_success(self, mock_session):
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)
        mock_session.head.return_value = _make_head_resp("sha256:ghcrdigest")
        result = fetch_digest("ghcr.io/owner/repo", "v1.0", "", "", "gh-token")
        assert result == "sha256:ghcrdigest"

    @patch.object(http_mod, "http_session")
    def test_ghcr_no_github_token_returns_none(self, mock_session):
        result = fetch_digest("ghcr.io/owner/repo", "v1.0", "", "", "")
        assert result is None
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_ghcr_registry_token_failure_returns_none(self, mock_session):
        mock_session.get.return_value = _make_get_resp({})  # PAT exchange yields nothing
        result = fetch_digest("ghcr.io/owner/repo", "v1.0", "", "", "gh-token")
        assert result is None
        mock_session.head.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_lscr_io_uses_lscr_host(self, mock_session):
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)
        mock_session.head.return_value = _make_head_resp("sha256:lscr")
        result = fetch_digest("lscr.io/linuxserver/sonarr", "v1.0", "", "", "gh-token")
        assert result == "sha256:lscr"
        token_url = mock_session.get.call_args_list[0][0][0]
        assert "lscr.io/token" in token_url

    @patch.object(http_mod, "http_session")
    def test_ghcr_full_url_with_scheme(self, mock_session):
        """A scheme-qualified GHCR ref resolves its host via urlparse().hostname."""
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)
        mock_session.head.return_value = _make_head_resp("sha256:ghcrdigest")
        result = fetch_digest("https://ghcr.io/owner/repo", "v1.0", "", "", "gh-token")
        assert result == "sha256:ghcrdigest"
        token_url = mock_session.get.call_args_list[0][0][0]
        assert "ghcr.io/token" in token_url

    @patch.object(http_mod, "http_session")
    def test_unknown_registry_returns_none(self, mock_session):
        result = fetch_digest("myregistry.example.com/team/app", "v1.0", "", "", "")
        assert result is None
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_result_is_cached(self, mock_session):
        mock_session.get.return_value = _make_get_resp(_TOKEN_RESPONSE)
        mock_session.head.return_value = _make_head_resp("sha256:abc123")
        r1 = fetch_digest("nginx", "1.25.0", "", "", "")
        r2 = fetch_digest("nginx", "1.25.0", "", "", "")
        assert r1 == r2 == "sha256:abc123"
        # Second call served from cache — no extra HEAD.
        assert mock_session.head.call_count == 1


# ---------------------------------------------------------------------------
# fetch_platform_digest
# ---------------------------------------------------------------------------

class TestFetchPlatformDigest:
    @patch.object(http_mod, "http_session")
    def test_dockerhub_success(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "arm64", "", "", "")
        assert result == "sha256:arm64digest"

    @patch.object(http_mod, "http_session")
    def test_platform_not_present_returns_none(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "riscv64", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_schema_v2_without_mediatype(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_INDEX_WITHOUT_MEDIATYPE),
        ]
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        assert result == "sha256:amd64digest"

    @patch.object(http_mod, "http_session")
    def test_single_arch_manifest_returns_none(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_SINGLE_ARCH_RESPONSE),
        ]
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_http_error_returns_none(self, mock_session):
        error_resp = MagicMock()
        error_resp.raise_for_status.side_effect = requests.HTTPError("500")
        mock_session.get.side_effect = [_make_get_resp(_TOKEN_RESPONSE), error_resp]
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_generic_error_returns_none(self, mock_session):
        mock_session.get.side_effect = [_make_get_resp(_TOKEN_RESPONSE), ValueError("boom")]
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_dockerhub_token_failure_returns_none(self, mock_session):
        mock_session.get.return_value = _make_get_resp({})
        result = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        assert result is None

    @patch.object(http_mod, "http_session")
    def test_ghcr_success(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        result = fetch_platform_digest("ghcr.io/owner/repo", "v1.0", "linux", "amd64", "", "", "gh-token")
        assert result == "sha256:amd64digest"

    @patch.object(http_mod, "http_session")
    def test_ghcr_full_url_with_scheme(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        result = fetch_platform_digest(
            "https://ghcr.io/owner/repo", "v1.0", "linux", "arm64", "", "", "gh-token"
        )
        assert result == "sha256:arm64digest"
        token_url = mock_session.get.call_args_list[0][0][0]
        assert "ghcr.io/token" in token_url

    @patch.object(http_mod, "http_session")
    def test_ghcr_no_github_token_returns_none(self, mock_session):
        result = fetch_platform_digest("ghcr.io/owner/repo", "v1.0", "linux", "amd64", "", "", "")
        assert result is None
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_ghcr_registry_token_failure_returns_none(self, mock_session):
        mock_session.get.return_value = _make_get_resp({})
        result = fetch_platform_digest("ghcr.io/owner/repo", "v1.0", "linux", "amd64", "", "", "gh-token")
        assert result is None
        mock_session.get.assert_called_once()  # bailed after the failed token exchange

    @patch.object(http_mod, "http_session")
    def test_unknown_registry_returns_none(self, mock_session):
        result = fetch_platform_digest("myregistry.example.com/team/app", "v1.0", "linux", "amd64", "", "", "")
        assert result is None
        mock_session.get.assert_not_called()

    @patch.object(http_mod, "http_session")
    def test_result_is_cached(self, mock_session):
        mock_session.get.side_effect = [
            _make_get_resp(_TOKEN_RESPONSE),
            _make_get_resp(_MANIFEST_LIST_WITH_DIGESTS),
        ]
        r1 = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        r2 = fetch_platform_digest("nginx", "1.25.0", "linux", "amd64", "", "", "")
        assert r1 == r2 == "sha256:amd64digest"
        # Only the first call hit the network (token + manifest); the second was cached.
        assert mock_session.get.call_count == 2
