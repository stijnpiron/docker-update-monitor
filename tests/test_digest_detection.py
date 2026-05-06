"""Tests for digest-based update detection for rolling tags."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import config as config_mod
from app.models import UpdateInfo
from app.scanner import run_check, _resolve_digest_to_tag
from app.state import get_stored_digest, store_digest


def _make_container(name, image_tag, labels, has_image_tags=True):
    """Create a mock container."""
    c = MagicMock()
    c.name = name
    c.labels = labels
    if has_image_tags:
        c.image.tags = [image_tag]
    else:
        c.image.tags = []
    c.attrs = {"Config": {"Image": image_tag}}
    return c


class TestDigestFirstScanSilentStorage:
    """First scan with a rolling tag stores the digest silently (no update produced)."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:aabbcc1122334455")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_first_scan_stores_digest_no_update(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # Digest should be stored
        assert get_stored_digest("myimage", "latest") == "sha256:aabbcc1122334455"

        # No update should be dispatched (first scan is silent)
        if mock_notify.called:
            updates_arg = mock_notify.call_args[0][0]
            assert all(u.update_type != "digest" for u in updates_arg)


class TestDigestChangeDetected:
    """On subsequent scans, a digest change produces an UpdateInfo."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "1.2.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_digest_change_produces_update(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        # Pre-store an old digest
        store_digest("myimage", "latest", "sha256:olddigest000000000000")

        # New digest for 'latest', and the version resolution:
        # latest → sha256:newdigest111111111111
        # 1.2.0 → sha256:newdigest111111111111 (match!)
        def digest_side_effect(image, tag, *args, **kwargs):
            if tag == "latest":
                return "sha256:newdigest111111111111"
            if tag == "1.2.0":
                return "sha256:newdigest111111111111"
            return "sha256:otherdigest"

        mock_fetch_digest.side_effect = digest_side_effect

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # Should have dispatched an update
        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        digest_updates = [u for u in updates_arg if u.update_type == "digest"]
        assert len(digest_updates) == 1
        assert digest_updates[0].current_version == "latest"
        assert digest_updates[0].new_version == "1.2.0"
        assert digest_updates[0].image == "myimage"


class TestDigestVersionResolution:
    """The scanner resolves the new digest to a versioned tag."""

    @patch("app.scanner.fetch_digest")
    def test_resolve_digest_to_tag_finds_match(self, mock_fetch_digest):
        def digest_side_effect(image, tag, *args, **kwargs):
            if tag == "2.0.0":
                return "sha256:target_digest"
            return "sha256:other"

        mock_fetch_digest.side_effect = digest_side_effect

        result = _resolve_digest_to_tag(
            "myimage",
            "sha256:target_digest",
            ["1.0.0", "1.1.0", "2.0.0", "latest"],
            r"^(\d+)\.(\d+)\.(\d+)$",
        )
        assert result == "2.0.0"

    @patch("app.scanner.fetch_digest")
    def test_resolve_digest_no_match_returns_none(self, mock_fetch_digest):
        mock_fetch_digest.return_value = "sha256:different"

        result = _resolve_digest_to_tag(
            "myimage",
            "sha256:target_digest",
            ["1.0.0", "1.1.0"],
            r"^(\d+)\.(\d+)\.(\d+)$",
        )
        assert result is None


class TestDigestFallbackToShortDigest:
    """If no versioned tag matches, show the short digest."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_no_version_match_shows_short_digest(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        # Pre-store an old digest
        store_digest("myimage", "latest", "sha256:olddigest000000000000")

        # No versioned tag matches the new digest
        def digest_side_effect(image, tag, *args, **kwargs):
            if tag == "latest":
                return "sha256:abcdef123456789abcdef123456789abcdef123456789"
            return "sha256:nomatch"

        mock_fetch_digest.side_effect = digest_side_effect

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        digest_updates = [u for u in updates_arg if u.update_type == "digest"]
        assert len(digest_updates) == 1
        # Should show short digest (first 12 chars after "sha256:")
        assert digest_updates[0].new_version == "abcdef123456"


class TestDigestUnchanged:
    """When digest hasn't changed, no update is produced."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:samedigest000000000")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_unchanged_digest_no_update(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        # Same digest already stored
        store_digest("myimage", "latest", "sha256:samedigest000000000")

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # No digest updates should be dispatched
        if mock_notify.called:
            updates_arg = mock_notify.call_args[0][0]
            digest_updates = [u for u in updates_arg if u.update_type == "digest"]
            assert len(digest_updates) == 0


class TestSemverUnaffected:
    """Existing semver-based detection still works when tag matches regex."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "1.2.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_semver_still_works(self, mock_docker, mock_token, mock_fetch_tags, mock_notify):
        container = _make_container(
            "myapp", "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        # Need platform info for arch check
        container.image.attrs = {"Os": "linux", "Architecture": "amd64"}

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch("app.scanner.fetch_manifest_list", return_value=None):
            run_check()

        # Should have detected semver updates (not digest)
        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        assert all(u.update_type != "digest" for u in updates_arg)
        # Should have found patch/minor updates
        assert any(u.update_type in ("patch", "minor") for u in updates_arg)


class TestDigestStateDB:
    """Test the digest state storage functions."""

    def test_store_and_retrieve_digest(self):
        store_digest("nginx", "latest", "sha256:abc123")
        assert get_stored_digest("nginx", "latest") == "sha256:abc123"

    def test_get_nonexistent_digest_returns_none(self):
        assert get_stored_digest("nonexistent", "tag") is None

    def test_store_updates_existing(self):
        store_digest("nginx", "latest", "sha256:first")
        store_digest("nginx", "latest", "sha256:second")
        assert get_stored_digest("nginx", "latest") == "sha256:second"

    def test_different_tags_independent(self):
        store_digest("nginx", "latest", "sha256:latest_digest")
        store_digest("nginx", "nightly", "sha256:nightly_digest")
        assert get_stored_digest("nginx", "latest") == "sha256:latest_digest"
        assert get_stored_digest("nginx", "nightly") == "sha256:nightly_digest"


class TestDigestFetchFailure:
    """When digest fetch fails, a warning is produced."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value=None)
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_digest_fetch_failure_produces_warning(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify, caplog
    ):
        import logging

        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.WARNING):
            run_check()

        assert "Could not fetch digest" in caplog.text
