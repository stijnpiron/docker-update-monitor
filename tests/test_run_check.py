"""Unit tests for run_check() — Docker connection, container processing, and main() edge cases."""

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch
import pytest

from app import config as config_mod
from app import main as main_mod
from app.models import UpdateInfo
from app.scanner import run_check, _is_higher_version


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


class TestRunCheckDockerConnection:
    """Docker connection failure handling."""

    @patch("app.scanner.docker")
    def test_docker_connection_error_logs_and_returns(self, mock_docker, caplog):
        from docker.errors import DockerException
        import logging

        mock_docker.from_env.side_effect = DockerException("Cannot connect")

        with caplog.at_level(logging.ERROR):
            run_check()

        assert "Cannot connect to Docker" in caplog.text

    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_client_closed_after_successful_run(self, mock_docker, mock_token):
        """Docker client must be closed after a successful run_check() invocation."""
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = []

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_client.close.assert_called_once()

    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_client_closed_even_when_scan_raises(self, mock_docker, mock_token):
        """Docker client must be closed even when an unexpected exception occurs mid-scan."""
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.side_effect = RuntimeError("unexpected failure")

        with patch.object(config_mod, "GITHUB_TOKEN", ""), pytest.raises(RuntimeError):
            run_check()

        mock_client.close.assert_called_once()


class TestRunCheckContainerProcessing:
    """Container processing edge cases in run_check()."""

    @patch("app.scanner.fetch_all_tags", return_value=["1.1.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_container_without_image_tags_uses_fallback(self, mock_docker, mock_token, mock_fetch):
        """Container with no image.tags falls back to attrs Config.Image."""
        container = _make_container(
            "fallback-app", "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
            has_image_tags=False,
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_fetch.assert_called_once()

    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_container_with_no_image_ref_skipped(self, mock_docker, mock_token, caplog):
        """Container with empty image ref is skipped."""
        import logging

        container = MagicMock()
        container.name = "empty-ref"
        container.labels = {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"}
        container.image.tags = []
        container.attrs = {"Config": {"Image": ""}}

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.WARNING):
            run_check()

        assert "Cannot determine image reference" in caplog.text

    @patch("app.scanner.fetch_all_tags", return_value=[])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_no_tags_returned_skips_container(self, mock_docker, mock_token, mock_fetch, caplog):
        import logging

        container = _make_container(
            "no-tags", "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.WARNING):
            run_check()

        assert "No tags returned" in caplog.text

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_no_updates_found_logs_info(self, mock_docker, mock_token, mock_fetch, caplog):
        import logging

        container = _make_container(
            "up-to-date", "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.INFO):
            run_check()

        assert "No updates found" in caplog.text

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.notify")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_updates_found_calls_notify(self, mock_docker, mock_token, mock_notify, mock_fetch):
        container = _make_container(
            "outdated-app", "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_notify.assert_called_once()
        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1
        assert updates[0].new_version == "2.0.0"

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_digest_stripped_from_image_ref(self, mock_docker, mock_token, mock_fetch):
        """Image ref with digest has it stripped before parsing."""
        container = _make_container(
            "digest-app", "myimage:1.0.0@sha256:abc123",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # fetch_all_tags should be called with image_name without digest
        call_args = mock_fetch.call_args
        assert "@" not in call_args[0][0]

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_image_without_tag_defaults_to_latest(self, mock_docker, mock_token, mock_fetch):
        """Image ref without a tag defaults to 'latest'."""
        container = _make_container(
            "no-tag-app", "myimage",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        call_args = mock_fetch.call_args
        assert call_args[0][3] == "latest"  # current_tag argument

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_github_token_present_logs_info(self, mock_docker, mock_token, mock_fetch, caplog):
        import logging

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = []

        with patch.object(config_mod, "GITHUB_TOKEN", "gh_token"), caplog.at_level(logging.INFO):
            run_check()

        assert "GitHub token present" in caplog.text

    @patch("app.scanner.notify")
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_empty_containers_list(self, mock_docker, mock_token, mock_notify, caplog):
        """Empty container list results in no updates and notify still called."""
        import logging

        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = []

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.INFO):
            run_check()

        assert "Running containers: 0" in caplog.text
        mock_notify.assert_called_once()
        updates = mock_notify.call_args[0][0]
        assert updates == []

    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan", return_value=[])
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_mark_notified_not_called_when_no_categorized(self, mock_docker, mock_token,
                                                          mock_fetch, mock_scan, mock_mark):
        """mark_notified() should NOT be called when process_scan returns empty."""
        container = _make_container(
            "app", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_mark.assert_not_called()

    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_mark_notified_called_when_categorized_nonempty(self, mock_docker, mock_token,
                                                            mock_fetch, mock_scan, mock_mark):
        """mark_notified() should be called when process_scan returns updates."""
        from app.models import UpdateInfo
        categorized = [UpdateInfo(
            container_name="app", service_name="app", stack="stack",
            image="nginx", current_version="1.0.0", new_version="2.0.0",
            update_type="major", status="new",
        )]
        mock_scan.return_value = categorized

        container = _make_container(
            "app", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        mock_mark.assert_called_once()

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_stack_fallback_to_standalone(self, mock_docker, mock_token, mock_fetch, caplog):
        """Container without stack label or compose project defaults to 'standalone'."""
        import logging

        container = _make_container(
            "solo-app", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.INFO):
            run_check()

        assert "stack=standalone" in caplog.text

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_cache_different_versions_same_image(self, mock_docker, mock_token, mock_fetch):
        """Two containers running different versions of same image should fetch tags independently."""
        container1 = _make_container(
            "app1", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        container2 = _make_container(
            "app2", "nginx:1.1.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container1, container2]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # fetch_all_tags should be called twice with different current_tag
        assert mock_fetch.call_count == 2
        calls = mock_fetch.call_args_list
        assert calls[0][0][3] == "1.0.0"  # current_tag for first
        assert calls[1][0][3] == "1.1.0"  # current_tag for second

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "1.1.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_cache_same_version_fetches_once(self, mock_docker, mock_token, mock_fetch):
        """Two containers with same image and version should fetch only once."""
        container1 = _make_container(
            "app1", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        container2 = _make_container(
            "app2", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container1, container2]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        # Only one fetch due to cache hit
        assert mock_fetch.call_count == 1

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_invalid_regex_skips_and_warns(self, mock_docker, mock_token, mock_fetch, caplog):
        """Container with invalid regex pattern should be skipped with a warning."""
        import logging

        container = _make_container(
            "bad-regex", "nginx:1.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+["},  # invalid regex
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""), caplog.at_level(logging.WARNING):
            run_check()

        assert "Invalid tag-regex" in caplog.text
        mock_fetch.assert_not_called()


class TestRunCheckCooldown:
    """Cooldown suppression behaviour in run_check()."""

    _CONTAINER_NAME = "app"
    _PATTERN = r"^(\d+)\.(\d+)\.(\d+)$"

    def _make_update(self, first_seen_at: str) -> UpdateInfo:
        return UpdateInfo(
            container_name=self._CONTAINER_NAME,
            service_name=self._CONTAINER_NAME,
            stack="stack",
            image="nginx",
            current_version="1.0.0",
            new_version="2.0.0",
            update_type="major",
            status="new",
            first_seen_at=first_seen_at,
        )

    def _mock_docker(self, mock_docker, labels=None):
        if labels is None:
            labels = {f"docker-update-monitor.tag-regex": self._PATTERN}
        container = _make_container(self._CONTAINER_NAME, "nginx:1.0.0", labels)
        client = MagicMock()
        mock_docker.from_env.return_value = client
        client.containers.list.return_value = [container]

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_update_within_cooldown_not_notified(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """An update first seen moments ago is suppressed when a cooldown is set."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        # first_seen_at is 1 hour ago, cooldown is 12h → still within cooldown
        first_seen = (fixed_now - timedelta(hours=1)).isoformat()
        mock_scan.return_value = [self._make_update(first_seen)]

        self._mock_docker(mock_docker)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "12h"), \
             patch("app.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            run_check()

        mock_notify.assert_called_once()
        notified_updates = mock_notify.call_args[0][0]
        assert notified_updates == []
        mock_mark.assert_not_called()

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_update_past_cooldown_is_notified(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """An update first seen long enough ago passes through the cooldown filter."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        # first_seen_at is 13 hours ago, cooldown is 12h → past cooldown
        first_seen = (fixed_now - timedelta(hours=13)).isoformat()
        mock_scan.return_value = [self._make_update(first_seen)]

        self._mock_docker(mock_docker)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "12h"), \
             patch("app.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            run_check()

        mock_notify.assert_called_once()
        notified_updates = mock_notify.call_args[0][0]
        assert len(notified_updates) == 1
        assert notified_updates[0].new_version == "2.0.0"

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_per_container_label_overrides_global_cooldown(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """Per-container label cooldown takes precedence over global UPDATE_COOLDOWN."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        # first_seen_at is 6 hours ago; global=0 (no cooldown), label=12h → suppressed
        first_seen = (fixed_now - timedelta(hours=6)).isoformat()
        mock_scan.return_value = [self._make_update(first_seen)]

        labels = {
            "docker-update-monitor.tag-regex": self._PATTERN,
            "docker-update-monitor.update-cooldown": "12h",
        }
        self._mock_docker(mock_docker, labels=labels)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "0"), \
             patch("app.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            run_check()

        notified_updates = mock_notify.call_args[0][0]
        assert notified_updates == []

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_per_container_label_zero_bypasses_global_cooldown(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """Per-container label '0' disables cooldown even when global cooldown is set."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        # first_seen_at is 1 hour ago; global=12h, label=0 → not suppressed
        first_seen = (fixed_now - timedelta(hours=1)).isoformat()
        mock_scan.return_value = [self._make_update(first_seen)]

        labels = {
            "docker-update-monitor.tag-regex": self._PATTERN,
            "docker-update-monitor.update-cooldown": "0",
        }
        self._mock_docker(mock_docker, labels=labels)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "12h"), \
             patch("app.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            run_check()

        notified_updates = mock_notify.call_args[0][0]
        assert len(notified_updates) == 1

    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_invalid_cooldown_label_warns_and_uses_zero(
        self, mock_docker, mock_token, mock_fetch, caplog
    ):
        """An invalid per-container cooldown label logs a warning and uses no cooldown."""
        import logging

        labels = {
            "docker-update-monitor.tag-regex": self._PATTERN,
            "docker-update-monitor.update-cooldown": "bad-value",
        }
        self._mock_docker(mock_docker, labels=labels)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "0"), \
             caplog.at_level(logging.WARNING):
            run_check()

        assert "Invalid update-cooldown value" in caplog.text

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_resolved_updates_excluded_from_notification(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """Resolved updates are informational only and never enter the notification payload."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        # first_seen_at is 1 minute ago — well within any cooldown
        first_seen = (fixed_now - timedelta(minutes=1)).isoformat()
        resolved = UpdateInfo(
            container_name=self._CONTAINER_NAME,
            service_name=self._CONTAINER_NAME,
            stack="stack",
            image="nginx",
            current_version="2.0.0",
            new_version="2.0.0",
            update_type="major",
            status="resolved",
            first_seen_at=first_seen,
        )
        pending = UpdateInfo(
            container_name=self._CONTAINER_NAME,
            service_name=self._CONTAINER_NAME,
            stack="stack",
            image="redis",
            current_version="1.0.0",
            new_version="2.0.0",
            update_type="major",
            status="new",
            first_seen_at=first_seen,
        )
        mock_scan.return_value = [resolved, pending]

        self._mock_docker(mock_docker)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "0"), \
             patch("app.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            run_check()

        notified_updates = mock_notify.call_args[0][0]
        # Only the pending update is notified; the resolved one is dropped.
        assert [u.status for u in notified_updates] == ["new"]
        assert notified_updates[0].image == "redis"

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_resolved_only_scan_sends_empty_payload(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """A scan whose only change is a resolution notifies with an empty update list."""
        fixed_now = datetime(2026, 1, 10, 12, 0, 0, tzinfo=timezone.utc)
        resolved = UpdateInfo(
            container_name=self._CONTAINER_NAME,
            service_name=self._CONTAINER_NAME,
            stack="stack",
            image="nginx",
            current_version="2.0.0",
            new_version="2.0.0",
            update_type="major",
            status="resolved",
            first_seen_at=(fixed_now - timedelta(minutes=1)).isoformat(),
        )
        mock_scan.return_value = [resolved]

        self._mock_docker(mock_docker)
        with patch.object(config_mod, "GITHUB_TOKEN", ""), \
             patch.object(config_mod, "UPDATE_COOLDOWN", "0"), \
             patch("app.scanner.datetime") as mock_dt:
            mock_dt.now.return_value = fixed_now
            mock_dt.fromisoformat.side_effect = datetime.fromisoformat
            run_check()

        # notify() is still called, but with no updates — email/webhook layers
        # treat an empty payload as "nothing to send".
        notified_updates = mock_notify.call_args[0][0]
        assert notified_updates == []
        # Nothing was marked notified either.
        mock_mark.assert_not_called()


class TestIsHigherVersion:
    """Unit tests for the _is_higher_version() semver comparison helper."""

    def test_higher_major_wins(self):
        assert _is_higher_version("10.0.0", "9.0.0") is True

    def test_lower_major_loses(self):
        assert _is_higher_version("9.0.0", "10.0.0") is False

    def test_equal_versions_not_higher(self):
        assert _is_higher_version("1.2.3", "1.2.3") is False

    def test_higher_minor_wins(self):
        assert _is_higher_version("1.10.0", "1.9.0") is True

    def test_higher_patch_wins(self):
        assert _is_higher_version("1.0.10", "1.0.9") is True

    def test_none_candidate_is_not_higher(self):
        assert _is_higher_version(None, "1.0.0") is False

    def test_none_current_makes_any_candidate_higher(self):
        assert _is_higher_version("1.0.0", None) is True

    def test_both_none_not_higher(self):
        assert _is_higher_version(None, None) is False

    def test_digest_fallback_uses_string_comparison(self):
        # Non-numeric segments fall back to string comparison
        assert _is_higher_version("sha256:bbb", "sha256:aaa") is True
        assert _is_higher_version("sha256:aaa", "sha256:bbb") is False


class TestRunCheckDeduplication:
    """Deduplication of updates with the same (container, image, update_type) key."""

    def _make_update(self, container_name, image, update_type, new_version, current_version="1.0.0"):
        return UpdateInfo(
            container_name=container_name,
            service_name=container_name,
            stack="stack",
            image=image,
            current_version=current_version,
            new_version=new_version,
            update_type=update_type,
            status="new",
        )

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_duplicate_key_keeps_highest_version(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """Two entries with the same key but different new_version → only highest is kept."""
        lower = self._make_update("app", "nginx", "minor", "1.1.0")
        higher = self._make_update("app", "nginx", "minor", "1.2.0")
        mock_scan.return_value = [lower, higher]

        container = _make_container("app", "nginx:1.0.0",
                                    {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"})
        client = MagicMock()
        mock_docker.from_env.return_value = client
        client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1
        assert updates[0].new_version == "1.2.0"

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_duplicate_key_order_independent(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """Highest version wins regardless of iteration order (higher entry first)."""
        higher = self._make_update("app", "nginx", "minor", "1.2.0")
        lower = self._make_update("app", "nginx", "minor", "1.1.0")
        mock_scan.return_value = [higher, lower]

        container = _make_container("app", "nginx:1.0.0",
                                    {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"})
        client = MagicMock()
        mock_docker.from_env.return_value = client
        client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1
        assert updates[0].new_version == "1.2.0"

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_semver_crossing_digit_boundary(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """9.0.0 must not beat 10.0.0 — string '9' > '10' but int 9 < 10."""
        lower = self._make_update("app", "nginx", "major", "9.0.0")
        higher = self._make_update("app", "nginx", "major", "10.0.0")
        # 9.0.0 comes after 10.0.0 in the list — string comparison would pick 9.0.0 wrong
        mock_scan.return_value = [higher, lower]

        container = _make_container("app", "nginx:1.0.0",
                                    {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"})
        client = MagicMock()
        mock_docker.from_env.return_value = client
        client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        updates = mock_notify.call_args[0][0]
        assert len(updates) == 1
        assert updates[0].new_version == "10.0.0"

    @patch("app.scanner.notify")
    @patch("app.scanner.mark_notified")
    @patch("app.scanner.process_scan")
    @patch("app.scanner.fetch_all_tags", return_value=["1.0.0", "2.0.0"])
    @patch("app.scanner.get_dockerhub_token", return_value="token")
    @patch("app.scanner.docker")
    def test_different_keys_both_kept(
        self, mock_docker, mock_token, mock_fetch, mock_scan, mock_mark, mock_notify
    ):
        """Entries with different keys are all kept."""
        u1 = self._make_update("app1", "nginx", "minor", "1.1.0")
        u2 = self._make_update("app2", "redis", "major", "2.0.0")
        mock_scan.return_value = [u1, u2]

        container = _make_container("app1", "nginx:1.0.0",
                                    {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"})
        client = MagicMock()
        mock_docker.from_env.return_value = client
        client.containers.list.return_value = [container]

        with patch.object(config_mod, "GITHUB_TOKEN", ""):
            run_check()

        updates = mock_notify.call_args[0][0]
        assert len(updates) == 2


class TestMainEdgeCases:
    """Edge cases in main()."""

    def test_invalid_cron_exits(self):
        with patch.object(config_mod, "CRON_SCHEDULE", "invalid cron"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()
        assert exc_info.value.code == 1

    @patch("app.main.run_check")
    def test_dry_run_logs_mode(self, mock_run_check, caplog):
        import logging

        with patch.object(config_mod, "DRY_RUN", True), \
             patch.object(config_mod, "CRON_SCHEDULE", "0 * * * *"), \
             patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch("app.main.time.sleep", side_effect=InterruptedError), \
             caplog.at_level(logging.INFO), \
             pytest.raises(InterruptedError):
            main_mod.main()

        assert "DRY_RUN mode active" in caplog.text
