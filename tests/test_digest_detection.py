"""Tests for digest-based update detection for rolling tags."""

from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import config as config_mod
from app.models import UpdateInfo
from app.scanner import run_check, _resolve_digest_to_tag, _extract_local_digest
from app.state import get_stored_digest, store_digest, process_scan, get_active_updates


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

    @patch("app.scanner.fetch_digest")
    def test_resolve_digest_finds_git_hash_tag(self, mock_fetch_digest):
        def digest_side_effect(image, tag, *args, **kwargs):
            if tag == "sha-675e77e":
                return "sha256:newdigest111"
            return "sha256:other"

        mock_fetch_digest.side_effect = digest_side_effect

        result = _resolve_digest_to_tag(
            "ghcr.io/myorg/myimage",
            "sha256:newdigest111",
            ["edge", "sha-675e77e"],
            r"^(\d+)\.(\d+)\.(\d+)$",
            current_tag="edge",
        )
        assert result == "sha-675e77e"

    @patch("app.scanner.fetch_digest")
    def test_resolve_digest_excludes_current_tag_from_fallback(self, mock_fetch_digest):
        mock_fetch_digest.return_value = "sha256:target"

        result = _resolve_digest_to_tag(
            "myimage",
            "sha256:target",
            ["edge"],
            r"^(\d+)\.(\d+)\.(\d+)$",
            current_tag="edge",
        )
        assert result is None

    @patch("app.scanner.fetch_digest")
    def test_resolve_digest_excludes_current_tag_from_pattern_matches(self, mock_fetch_digest):
        """Regression: pattern phase must skip current_tag, mirroring the fallback phase."""
        mock_fetch_digest.return_value = "sha256:target"

        result = _resolve_digest_to_tag(
            "myimage",
            "sha256:target",
            ["1.2.3"],
            r"^(\d+)\.(\d+)\.(\d+)$",
            current_tag="1.2.3",
        )
        assert result is None

    @patch("app.scanner.fetch_digest")
    def test_resolve_digest_prefers_git_hash_over_other_rolling_tags(self, mock_fetch_digest):
        def digest_side_effect(image, tag, *args, **kwargs):
            if tag in ("latest", "sha-abcdef1"):
                return "sha256:target"
            return "sha256:other"

        mock_fetch_digest.side_effect = digest_side_effect

        result = _resolve_digest_to_tag(
            "myimage",
            "sha256:target",
            ["edge", "latest", "sha-abcdef1"],
            r"^(\d+)\.(\d+)\.(\d+)$",
            current_tag="edge",
        )
        assert result == "sha-abcdef1"


class TestDigestGitHashTagIntegration:
    """End-to-end: a rolling-tag container shows the git-hash tag as new version."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.fetch_all_tags", return_value=["edge", "sha-675e77e"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_digest_change_shows_git_hash_tag(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        container = _make_container(
            "myapp", "ghcr.io/myorg/myimage:edge",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        store_digest("ghcr.io/myorg/myimage", "edge", "sha256:olddigest000000000000")

        def digest_side_effect(image, tag, *args, **kwargs):
            if tag in ("edge", "sha-675e77e"):
                return "sha256:newdigest111111111111"
            return "sha256:other"

        mock_fetch_digest.side_effect = digest_side_effect

        with patch.object(config_mod, "GITHUB_TOKEN", "ghtoken"):
            run_check()

        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        digest_updates = [u for u in updates_arg if u.update_type == "digest"]
        assert len(digest_updates) == 1
        assert digest_updates[0].new_version == "sha-675e77e"
        assert digest_updates[0].current_version == "edge"


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
        # Should show full digest reference (usable as image@sha256:... reference)
        assert digest_updates[0].new_version == "sha256:abcdef123456789abcdef123456789abcdef123456789"


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


class TestDigestAutoResolveAfterRepull:
    """After repulling the updated image, the next scan resolves the pending update."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:newdigest111111")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_repulled_container_resolves_pending_update(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        """The pending digest update is auto-resolved when the container runs the new image."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        container.image.attrs = {"RepoDigests": ["myimage@sha256:newdigest111111"]}

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        # Simulate prior scan: digest was stored and update entry created
        store_digest("myimage", "latest", "sha256:newdigest111111")
        process_scan([UpdateInfo(
            container_name="myapp", service_name="", stack="standalone",
            image="myimage", current_version="latest",
            new_version="sha256:newdigest111111", update_type="digest",
        )])
        assert len(get_active_updates()) == 1

        # Registry still returns the same new digest (unchanged from scanner's perspective)
        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # Pending update should be resolved — container is running the updated image
        assert len(get_active_updates()) == 0

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:newdigest111111")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_not_repulled_container_keeps_pending_update(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        """Update stays active when the container is still running the old image."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        # Container still has the old image in RepoDigests
        container.image.attrs = {"RepoDigests": ["myimage@sha256:olddigest000000"]}

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        store_digest("myimage", "latest", "sha256:newdigest111111")
        process_scan([UpdateInfo(
            container_name="myapp", service_name="", stack="standalone",
            image="myimage", current_version="latest",
            new_version="sha256:newdigest111111", update_type="digest",
        )])
        assert len(get_active_updates()) == 1

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # Update should still be active
        assert len(get_active_updates()) == 1


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


# ---------------------------------------------------------------------------
# Tests for _extract_local_digest helper
# ---------------------------------------------------------------------------

class TestExtractLocalDigest:
    def test_standard_format(self):
        assert _extract_local_digest(["nginx@sha256:abc123"]) == "sha256:abc123"

    def test_registry_prefixed_format(self):
        assert _extract_local_digest(
            ["ghcr.io/myorg/myimage@sha256:def456"]
        ) == "sha256:def456"

    def test_multiple_entries_returns_first(self):
        result = _extract_local_digest([
            "nginx@sha256:first111",
            "docker.io/library/nginx@sha256:second222",
        ])
        assert result == "sha256:first111"

    def test_empty_list(self):
        assert _extract_local_digest([]) is None

    def test_no_at_sign(self):
        assert _extract_local_digest(["nginx:latest"]) is None

    def test_non_sha256_digest_skipped(self):
        assert _extract_local_digest(["nginx@md5:abc"]) is None


# ---------------------------------------------------------------------------
# Tests for mode=digest label (explicit digest mode using RepoDigests)
# ---------------------------------------------------------------------------

class TestDigestModeLabel:
    """mode=digest label compares local RepoDigests against the remote registry digest."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:local111")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_digests_match_no_update(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify
    ):
        """No update when local RepoDigests matches remote digest."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        container.image.attrs = {"RepoDigests": ["myimage@sha256:local111"], "Os": "", "Architecture": ""}

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        if mock_notify.called:
            updates_arg = mock_notify.call_args[0][0]
            digest_updates = [u for u in updates_arg if u.update_type == "digest"]
            assert len(digest_updates) == 0

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:remote222")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_digests_differ_reports_update(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify
    ):
        """Update reported on first scan when local digest differs from remote."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:local111"],
            "Os": "",
            "Architecture": "",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch("app.scanner.fetch_platform_digest", return_value=None):
            run_check()

        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        digest_updates = [u for u in updates_arg if u.update_type == "digest"]
        assert len(digest_updates) == 1
        assert digest_updates[0].current_version == "latest"
        assert digest_updates[0].new_version == "sha256:remote222"
        assert digest_updates[0].image == "myimage"

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0", "latest"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_with_tag_regex_resolves_to_version(
        self, mock_docker, mock_token, mock_fetch_tags, mock_fetch_digest, mock_notify
    ):
        """When tag-regex is also set, the new digest resolves to a versioned tag."""
        container = _make_container(
            "myapp", "myimage:latest",
            {
                "docker-update-monitor.mode": "digest",
                "docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$",
            },
        )
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:olddigest000"],
            "Os": "",
            "Architecture": "",
        }

        def digest_side_effect(image, tag, *args, **kwargs):
            if tag == "latest":
                return "sha256:newdigest111"
            if tag == "1.1.0":
                return "sha256:newdigest111"
            return "sha256:other"

        mock_fetch_digest.side_effect = digest_side_effect

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch("app.scanner.fetch_platform_digest", return_value=None):
            run_check()

        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        digest_updates = [u for u in updates_arg if u.update_type == "digest"]
        assert len(digest_updates) == 1
        assert digest_updates[0].new_version == "1.1.0"

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:remote222")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_no_repo_digests_produces_warning(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify, caplog
    ):
        """A warning is produced when RepoDigests is empty."""
        import logging

        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        container.image.attrs = {"RepoDigests": [], "Os": "", "Architecture": ""}

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.WARNING):
            run_check()

        assert "No RepoDigests" in caplog.text
        if mock_notify.called:
            updates_arg = mock_notify.call_args[0][0]
            digest_updates = [u for u in updates_arg if u.update_type == "digest"]
            assert len(digest_updates) == 0

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value=None)
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_remote_digest_fetch_failure_produces_warning(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify, caplog
    ):
        """A warning is produced when the remote digest cannot be fetched."""
        import logging

        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:local111"],
            "Os": "",
            "Architecture": "",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.WARNING):
            run_check()

        assert "Could not fetch remote digest" in caplog.text

    @patch("app.scanner.notify")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_mode_digest_without_tag_regex(
        self, mock_docker, mock_token, mock_notify
    ):
        """mode=digest works without a tag-regex label."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:local111"],
            "Os": "",
            "Architecture": "",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch("app.scanner.fetch_digest", return_value="sha256:local111"):
            run_check()

        # Digests match — no update expected
        if mock_notify.called:
            updates_arg = mock_notify.call_args[0][0]
            digest_updates = [u for u in updates_arg if u.update_type == "digest"]
            assert len(digest_updates) == 0

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_multiarch_platform_digest_match_suppresses_update(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify
    ):
        """No update when manifest list digest differs but platform digest matches local."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        # Local RepoDigests has the platform-specific digest
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:platform-amd64"],
            "Os": "linux",
            "Architecture": "amd64",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        # Remote returns a different manifest list digest (another platform was updated)
        mock_fetch_digest.return_value = "sha256:new-manifest-list"

        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch("app.scanner.fetch_platform_digest", return_value="sha256:platform-amd64"):
            run_check()

        # Platform-specific digest unchanged → no update
        if mock_notify.called:
            updates_arg = mock_notify.call_args[0][0]
            digest_updates = [u for u in updates_arg if u.update_type == "digest"]
            assert len(digest_updates) == 0

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_multiarch_platform_digest_changed_reports_update(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify
    ):
        """Update reported when the platform-specific digest also changed."""
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:old-platform-amd64"],
            "Os": "linux",
            "Architecture": "amd64",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        mock_fetch_digest.return_value = "sha256:new-manifest-list"

        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch("app.scanner.fetch_platform_digest", return_value="sha256:new-platform-amd64"):
            run_check()

        assert mock_notify.called
        updates_arg = mock_notify.call_args[0][0]
        digest_updates = [u for u in updates_arg if u.update_type == "digest"]
        assert len(digest_updates) == 1
        assert digest_updates[0].new_version == "sha256:new-manifest-list"


class TestDigestModeAutoResolveAfterRepull:
    """mode=digest containers auto-resolve after repull, same as implicit digest mode."""

    @patch("app.scanner.notify")
    @patch("app.scanner.fetch_digest", return_value="sha256:remote222")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_repulled_container_resolves_pending_update(
        self, mock_docker, mock_token, mock_fetch_digest, mock_notify
    ):
        container = _make_container(
            "myapp", "myimage:latest",
            {"docker-update-monitor.mode": "digest"},
        )
        # Container now runs the updated image
        container.image.attrs = {
            "RepoDigests": ["myimage@sha256:remote222"],
            "Os": "",
            "Architecture": "",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        # Pre-create a pending digest update in the DB (simulating prior scan)
        store_digest("myimage", "latest", "sha256:remote222")
        process_scan([UpdateInfo(
            container_name="myapp", service_name="", stack="standalone",
            image="myimage", current_version="latest",
            new_version="sha256:remote222", update_type="digest",
        )])
        assert len(get_active_updates()) == 1

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # Pending update should be resolved
        assert len(get_active_updates()) == 0


class TestFetchPlatformDigest:
    """Unit tests for the fetch_platform_digest function in manifest.py."""

    def test_returns_platform_specific_digest(self):
        from app.registry.manifest import _fetch_platform_digest_from_url

        manifest_data = {
            "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
            "manifests": [
                {"digest": "sha256:amd64digest", "platform": {"os": "linux", "architecture": "amd64"}},
                {"digest": "sha256:arm64digest", "platform": {"os": "linux", "architecture": "arm64"}},
            ],
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = manifest_data

        with patch("app.registry.manifest._http") as mock_http:
            mock_http.http_session.get.return_value = mock_resp
            result = _fetch_platform_digest_from_url(
                "https://example.com/v2/img/manifests/latest",
                {},
                "linux", "amd64",
            )

        assert result == "sha256:amd64digest"

    def test_returns_none_for_missing_platform(self):
        from app.registry.manifest import _fetch_platform_digest_from_url

        manifest_data = {
            "mediaType": "application/vnd.docker.distribution.manifest.list.v2+json",
            "manifests": [
                {"digest": "sha256:arm64digest", "platform": {"os": "linux", "architecture": "arm64"}},
            ],
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = manifest_data

        with patch("app.registry.manifest._http") as mock_http:
            mock_http.http_session.get.return_value = mock_resp
            result = _fetch_platform_digest_from_url(
                "https://example.com/v2/img/manifests/latest",
                {},
                "linux", "amd64",
            )

        assert result is None

    def test_returns_none_for_single_arch_image(self):
        from app.registry.manifest import _fetch_platform_digest_from_url

        manifest_data = {
            "mediaType": "application/vnd.docker.distribution.manifest.v2+json",
            "layers": [],
        }

        mock_resp = MagicMock()
        mock_resp.json.return_value = manifest_data

        with patch("app.registry.manifest._http") as mock_http:
            mock_http.http_session.get.return_value = mock_resp
            result = _fetch_platform_digest_from_url(
                "https://example.com/v2/img/manifests/latest",
                {},
                "linux", "amd64",
            )

        assert result is None


# ---------------------------------------------------------------------------
# Platform digest exception handling — narrowed from broad Exception
# ---------------------------------------------------------------------------

class TestPlatformDigestExceptionHandling:
    """Verify that only specific exceptions are caught in the platform digest block."""

    def _setup_digest_mismatch(self, mock_docker, local_digest="sha256:aabbcc", remote_digest="sha256:ddeeff"):
        """Configure a mock docker client where local != remote digest, triggering platform check."""
        container = MagicMock()
        container.name = "myapp"
        container.labels = {"docker-update-monitor.mode": "digest"}
        container.image.tags = ["nginx:latest"]
        container.attrs = {"Config": {"Image": "nginx:latest"}}
        container.image.attrs = {
            "RepoDigests": [f"nginx@{local_digest}"],
            "Os": "linux",
            "Architecture": "amd64",
        }

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]
        return container, local_digest, remote_digest

    @patch("app.scanner.fetch_platform_digest")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_requests_exception_is_caught(self, mock_docker, _token, mock_fetch_digest, mock_platform_digest):
        """requests.RequestException during platform digest check is caught and logged."""
        import requests

        container, local, remote = self._setup_digest_mismatch(mock_docker)
        mock_fetch_digest.return_value = remote
        mock_platform_digest.side_effect = requests.ConnectionError("network down")

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "DOCKERHUB_USER", ""), \
             patch.object(config_mod, "DOCKERHUB_PASS", ""):
            run_check()

        mock_notify.assert_called_once()

    @patch("app.scanner.fetch_platform_digest")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_key_error_is_caught(self, mock_docker, _token, mock_fetch_digest, mock_platform_digest):
        """KeyError during platform digest check is caught and logged."""
        container, local, remote = self._setup_digest_mismatch(mock_docker)
        mock_fetch_digest.return_value = remote
        mock_platform_digest.side_effect = KeyError("missing_key")

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "DOCKERHUB_USER", ""), \
             patch.object(config_mod, "DOCKERHUB_PASS", ""):
            run_check()

        mock_notify.assert_called_once()

    @patch("app.scanner.fetch_platform_digest")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_docker_api_error_is_caught(self, mock_docker, _token, mock_fetch_digest, mock_platform_digest):
        """docker.errors.APIError during platform digest check is caught and logged."""
        import docker as docker_mod

        container, local, remote = self._setup_digest_mismatch(mock_docker)
        mock_fetch_digest.return_value = remote
        mock_platform_digest.side_effect = docker_mod.errors.APIError("docker error")

        with patch("app.scanner.notify") as mock_notify, \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "DOCKERHUB_USER", ""), \
             patch.object(config_mod, "DOCKERHUB_PASS", ""):
            run_check()

        mock_notify.assert_called_once()

    @patch("app.scanner.fetch_platform_digest")
    @patch("app.scanner.fetch_digest")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_programming_error_propagates(self, mock_docker, _token, mock_fetch_digest, mock_platform_digest):
        """Programming errors (e.g. AttributeError) must not be swallowed."""
        container, local, remote = self._setup_digest_mismatch(mock_docker)
        mock_fetch_digest.return_value = remote
        mock_platform_digest.side_effect = AttributeError("bad attribute")

        with patch("app.scanner.notify"), \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "DOCKERHUB_USER", ""), \
             patch.object(config_mod, "DOCKERHUB_PASS", ""):
            with pytest.raises(AttributeError, match="bad attribute"):
                run_check()
