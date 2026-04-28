"""Unit tests for early regex validation in run_check()."""

from unittest.mock import patch, MagicMock
import pytest

from app.scanner import run_check


def _make_container(name, image_tag, labels):
    """Create a mock container with the given name, image tag, and labels."""
    c = MagicMock()
    c.name = name
    c.labels = labels
    c.image.tags = [image_tag]
    c.attrs = {"Config": {"Image": image_tag}}
    return c


class TestInvalidRegexRejectedEarly:
    """Invalid regex patterns are caught before any registry API calls."""

    @patch("app.scanner.fetch_all_tags")
    @patch("app.scanner.get_dockerhub_token", return_value="fake-token")
    @patch("app.scanner.docker")
    def test_invalid_regex_skips_container(self, mock_docker, mock_token, mock_fetch):
        """Container with invalid regex is skipped; fetch_all_tags never called."""
        container = _make_container(
            "bad-regex-app",
            "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": "[invalid(regex"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        run_check()

        mock_fetch.assert_not_called()

    @patch("app.scanner.fetch_all_tags")
    @patch("app.scanner.get_dockerhub_token", return_value="fake-token")
    @patch("app.scanner.docker")
    def test_invalid_regex_logs_warning(self, mock_docker, mock_token, mock_fetch, caplog):
        """A clear warning is logged naming the container and the invalid pattern."""
        container = _make_container(
            "bad-regex-app",
            "myimage:1.0.0",
            {"docker-update-monitor.tag-regex": "[bad("},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]

        import logging
        with caplog.at_level(logging.WARNING):
            run_check()

        assert any("bad-regex-app" in msg and "Invalid tag-regex" in msg for msg in caplog.messages)

    @patch("app.scanner.fetch_all_tags")
    @patch("app.scanner.get_dockerhub_token", return_value="fake-token")
    @patch("app.scanner.docker")
    def test_invalid_regex_does_not_block_other_containers(self, mock_docker, mock_token, mock_fetch):
        """Other containers with valid regex continue processing."""
        bad_container = _make_container(
            "bad-app",
            "badimage:1.0.0",
            {"docker-update-monitor.tag-regex": "[invalid("},
        )
        good_container = _make_container(
            "good-app",
            "goodimage:2.0.0",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [bad_container, good_container]
        mock_fetch.return_value = ["2.0.1"]

        run_check()

        mock_fetch.assert_called_once()


class TestValidRegexPassesThrough:
    """Valid regex patterns continue to work unchanged."""

    @patch("app.scanner.fetch_all_tags")
    @patch("app.scanner.get_dockerhub_token", return_value="fake-token")
    @patch("app.scanner.docker")
    def test_valid_regex_proceeds_to_fetch(self, mock_docker, mock_token, mock_fetch):
        """Container with valid regex triggers fetch_all_tags."""
        container = _make_container(
            "valid-app",
            "myimage:1.2.3",
            {"docker-update-monitor.tag-regex": r"^(\d+)\.(\d+)\.(\d+)$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]
        mock_fetch.return_value = ["1.2.4"]

        run_check()

        mock_fetch.assert_called_once()

    @patch("app.scanner.fetch_all_tags")
    @patch("app.scanner.get_dockerhub_token", return_value="fake-token")
    @patch("app.scanner.docker")
    def test_complex_valid_regex_passes(self, mock_docker, mock_token, mock_fetch):
        """Complex but valid regex patterns are accepted."""
        container = _make_container(
            "complex-app",
            "myimage:v1.2.3-beta",
            {"docker-update-monitor.tag-regex": r"^v(\d+)\.(\d+)\.(\d+)(?:-\w+)?$"},
        )
        mock_client = MagicMock()
        mock_docker.from_env.return_value = mock_client
        mock_client.containers.list.return_value = [container]
        mock_fetch.return_value = ["v1.2.4"]

        run_check()

        mock_fetch.assert_called_once()
