"""Unit tests for RUN_ON_STARTUP behavior in main()."""

import os
from unittest.mock import patch, MagicMock, call
import pytest

from app import config as config_mod
from app import main as main_mod


@pytest.fixture(autouse=True)
def _mock_health_server():
    """Prevent the dashboard from binding a real port during main() tests."""
    with patch("app.main.start_dashboard"):
        yield


class TestRunOnStartup:
    """Verify run_check() is called/skipped on startup based on RUN_ON_STARTUP."""

    @patch("app.main.run_check")
    @patch("app.main.time.sleep", side_effect=InterruptedError)
    def test_run_check_called_on_startup_when_enabled(self, mock_sleep, mock_run_check):
        """RUN_ON_STARTUP=true (default): run_check() is called before cron loop."""
        with patch.object(config_mod, "RUN_ON_STARTUP", True):
            with pytest.raises(InterruptedError):
                main_mod.main()
            mock_run_check.assert_called()

    @patch("app.main.run_check")
    @patch("app.main.time.sleep", side_effect=InterruptedError)
    def test_run_check_not_called_on_startup_when_disabled(self, mock_sleep, mock_run_check):
        """RUN_ON_STARTUP=false: run_check() is NOT called before cron loop."""
        with patch.object(config_mod, "RUN_ON_STARTUP", False):
            with pytest.raises(InterruptedError):
                main_mod.main()
            mock_run_check.assert_not_called()

    @patch("app.main.run_check")
    def test_shutdown_during_startup_check_exits_immediately(self, mock_run_check):
        """If shutdown is requested during the startup run_check, exit without entering the loop."""
        def set_shutdown():
            main_mod.shutdown_requested = True

        mock_run_check.side_effect = set_shutdown

        with patch.object(config_mod, "RUN_ON_STARTUP", True), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        mock_run_check.assert_called_once()


class TestCronLoop:
    """Tests covering the cron scheduling loop in main()."""

    @patch("app.main.run_check")
    @patch("app.main.time.sleep")
    def test_cron_loop_runs_check_and_schedules_next(self, mock_sleep, mock_run_check):
        """After wait expires, run_check is called and next_run is advanced."""
        call_count = 0

        def shutdown_after_first_check():
            nonlocal call_count
            call_count += 1
            if call_count >= 1:
                main_mod.shutdown_requested = True

        mock_run_check.side_effect = shutdown_after_first_check

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "* * * * *"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        mock_run_check.assert_called_once()

    @patch("app.main.run_check")
    @patch("app.main.time.sleep")
    def test_cron_loop_shutdown_during_sleep(self, mock_sleep, mock_run_check):
        """If shutdown is requested during the sleep loop, exits without calling run_check."""
        def set_shutdown(*args):
            main_mod.shutdown_requested = True

        mock_sleep.side_effect = set_shutdown

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "* * * * *"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        mock_run_check.assert_not_called()

    @patch("app.main.run_check")
    @patch("app.main.time.sleep")
    def test_cron_loop_completes_two_checks(self, mock_sleep, mock_run_check):
        """Verifies the loop advances next_run after each check."""
        call_count = 0

        def shutdown_after_second_check():
            nonlocal call_count
            call_count += 1
            if call_count >= 2:
                main_mod.shutdown_requested = True

        mock_run_check.side_effect = shutdown_after_second_check

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "* * * * *"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        assert mock_run_check.call_count == 2
