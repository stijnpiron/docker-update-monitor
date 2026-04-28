"""Unit tests for RUN_ON_STARTUP behavior in main()."""

import os
from unittest.mock import patch, MagicMock
import pytest

from app import config as config_mod
from app import main as main_mod


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
