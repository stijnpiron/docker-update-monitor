"""Tests for the Docker healthcheck script."""

import sys
import urllib.error
from unittest.mock import patch, MagicMock
import runpy

import pytest


class TestHealthcheck:
    """Tests for app/healthcheck.py module behavior."""

    def _run_healthcheck(self):
        """Execute the healthcheck module as a script."""
        runpy.run_module("app.healthcheck", run_name="__main__")

    @patch("urllib.request.urlopen")
    def test_successful_request_exits_normally(self, mock_urlopen):
        mock_urlopen.return_value = MagicMock()
        self._run_healthcheck()
        mock_urlopen.assert_called_with("http://localhost:8080/health")

    @patch("urllib.request.urlopen")
    def test_http_error_exits_with_1(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.HTTPError(
            url="http://localhost:8080/health",
            code=503,
            msg="Service Unavailable",
            hdrs={},
            fp=None,
        )
        with pytest.raises(SystemExit) as exc_info:
            self._run_healthcheck()
        assert exc_info.value.code == 1

    @patch("urllib.request.urlopen")
    def test_connection_error_exits_with_1(self, mock_urlopen):
        mock_urlopen.side_effect = ConnectionRefusedError("Connection refused")
        with pytest.raises(SystemExit) as exc_info:
            self._run_healthcheck()
        assert exc_info.value.code == 1

    @patch("urllib.request.urlopen")
    def test_timeout_error_exits_with_1(self, mock_urlopen):
        mock_urlopen.side_effect = TimeoutError("timed out")
        with pytest.raises(SystemExit) as exc_info:
            self._run_healthcheck()
        assert exc_info.value.code == 1

    @patch("urllib.request.urlopen")
    def test_url_error_exits_with_1(self, mock_urlopen):
        mock_urlopen.side_effect = urllib.error.URLError("no host")
        with pytest.raises(SystemExit) as exc_info:
            self._run_healthcheck()
        assert exc_info.value.code == 1
