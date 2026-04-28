"""Unit tests for run_check() — Docker connection, container processing, and main() edge cases."""

from unittest.mock import MagicMock, patch
import pytest

from app import config as config_mod
from app import main as main_mod
from app.scanner import run_check


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
