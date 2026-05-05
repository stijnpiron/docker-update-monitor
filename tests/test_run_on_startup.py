"""Unit tests for RUN_ON_STARTUP behavior in main()."""

import os
from datetime import datetime, timezone
from unittest.mock import patch, MagicMock, call
import pytest

from app import config as config_mod
from app import main as main_mod
from app.dashboard import _scan_trigger


@pytest.fixture(autouse=True)
def _reset_scan_trigger():
    """Ensure _scan_trigger is cleared before and after every test."""
    _scan_trigger.clear()
    yield
    _scan_trigger.clear()


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

    @patch("app.main.update_state")
    @patch("app.main.run_check")
    @patch("app.main.time.sleep")
    def test_manual_trigger_does_not_skip_next_scheduled_run(self, mock_sleep, mock_run_check, mock_update_state):
        """Manual trigger must not advance the croniter past the already-scheduled next run.

        Regression test for: next scheduled run is not triggered, manual runs do work.

        Root cause: calling cron.get_next() after a manual trigger advances the shared
        croniter iterator, skipping the upcoming scheduled run. The fix recreates the
        croniter from the current time so next_run stays ≤ 1 cron period away.
        """
        sleep_calls = 0

        def sleep_fn(duration):
            nonlocal sleep_calls
            sleep_calls += 1
            # Fire manual scan on first sleep (still within the wait period)
            if sleep_calls == 1:
                _scan_trigger.set()
            # Shut down shortly after to keep the test fast
            elif sleep_calls > 10:
                main_mod.shutdown_requested = True

        mock_sleep.side_effect = sleep_fn

        before = datetime.now(timezone.utc)

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "0 * * * *"), \
             pytest.raises(SystemExit):
            main_mod.main()

        # Collect the next_check values passed to update_state:
        # index 0 = initial cron setup, index 1 = after manual trigger
        next_check_values = [
            c.kwargs["next_check"]
            for c in mock_update_state.call_args_list
            if "next_check" in c.kwargs
        ]
        assert len(next_check_values) >= 2, "Expected update_state(next_check=...) from both setup and manual trigger"

        after_trigger_next = next_check_values[1]
        delta_seconds = (after_trigger_next - before).total_seconds()

        # With the fix:  next_run is the NEXT scheduled hour → delta ≤ ~3600 s
        # Without fix:   cron.get_next() skips one period → delta ≈ 7200 s
        assert delta_seconds <= 3700, (
            f"next_check after manual trigger is {delta_seconds:.0f}s from test start, "
            f"expected ≤ 3700s (one hourly period). "
            f"This suggests the croniter iterator was incorrectly advanced past the scheduled run."
        )

    @patch("app.main.run_check")
    @patch("app.main.time.sleep")
    def test_manual_trigger_calls_run_check(self, mock_sleep, mock_run_check):
        """When _scan_trigger is set, run_check() is called for the manual scan."""
        _scan_trigger.set()
        mock_run_check.side_effect = lambda: setattr(main_mod, "shutdown_requested", True)

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "* * * * *"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        mock_run_check.assert_called_once()

    @patch("app.main.run_check")
    @patch("app.main.time.sleep")
    def test_manual_trigger_prevents_duplicate_scheduled_run(self, mock_sleep, mock_run_check):
        """A manual trigger sets triggered=True so the scheduled check does not also fire.

        After the manual run completes, the outer loop waits for the next scheduled time.
        The first time.sleep() call in that wait shuts down the loop — asserting that
        run_check() was only called once confirms the scheduled path was not entered.
        """
        _scan_trigger.set()

        def sleep_fn(duration):
            # First sleep occurs after manual trigger, while waiting for next_run
            main_mod.shutdown_requested = True

        mock_sleep.side_effect = sleep_fn

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "* * * * *"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        mock_run_check.assert_called_once()

    @patch("app.main.update_state")
    @patch("app.main.run_check")
    def test_shutdown_during_manual_scan_exits_without_rescheduling(self, mock_run_check, mock_update_state):
        """Shutdown inside a manual run_check exits cleanly and skips the croniter reset."""
        _scan_trigger.set()

        def run_and_shutdown():
            main_mod.shutdown_requested = True

        mock_run_check.side_effect = run_and_shutdown

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             patch.object(config_mod, "CRON_SCHEDULE", "* * * * *"), \
             pytest.raises(SystemExit) as exc_info:
            main_mod.main()

        assert exc_info.value.code == 0
        # update_state(next_check=...) must only have been called once — at initial setup.
        # The manual trigger path skips it when shutdown is requested.
        next_check_calls = [
            c for c in mock_update_state.call_args_list if "next_check" in c.kwargs
        ]
        assert len(next_check_calls) == 1, (
            "update_state(next_check=...) should only be called at startup, "
            "not after a manual scan that triggers shutdown"
        )


class TestStartupPersistence:
    """Tests for last_check restoration from persistent storage."""

    @patch("app.main.run_check")
    @patch("app.main.time.sleep", side_effect=InterruptedError)
    @patch("app.main.update_state")
    @patch("app.main.load_last_check")
    def test_persisted_last_check_restored_on_startup(
        self, mock_load, mock_update_state, mock_sleep, mock_run_check
    ):
        """A valid persisted ISO timestamp is parsed and passed to update_state."""
        mock_load.return_value = "2026-01-15T10:30:00+00:00"

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             pytest.raises(InterruptedError):
            main_mod.main()

        last_check_calls = [
            c for c in mock_update_state.call_args_list if "last_check" in c.kwargs
        ]
        assert len(last_check_calls) == 1
        restored = last_check_calls[0].kwargs["last_check"]
        assert restored == datetime(2026, 1, 15, 10, 30, 0, tzinfo=timezone.utc)

    @patch("app.main.run_check")
    @patch("app.main.time.sleep", side_effect=InterruptedError)
    @patch("app.main.update_state")
    @patch("app.main.load_last_check")
    def test_invalid_persisted_last_check_silently_ignored(
        self, mock_load, mock_update_state, mock_sleep, mock_run_check
    ):
        """A malformed persisted timestamp is swallowed and main() continues normally."""
        mock_load.return_value = "not-a-valid-datetime"

        with patch.object(config_mod, "RUN_ON_STARTUP", False), \
             pytest.raises(InterruptedError):
            main_mod.main()

        last_check_calls = [
            c for c in mock_update_state.call_args_list if "last_check" in c.kwargs
        ]
        assert last_check_calls == [], "update_state(last_check=...) must not be called for invalid input"
