"""Unit tests for RUN_ON_STARTUP behavior in main()."""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from unittest.mock import patch, MagicMock
import pytest


class TestRunOnStartup:
    """Verify run_check() is called/skipped on startup based on RUN_ON_STARTUP."""

    @patch("monitor.run_check")
    @patch("monitor.time.sleep", side_effect=InterruptedError)
    def test_run_check_called_on_startup_when_enabled(self, mock_sleep, mock_run_check):
        """RUN_ON_STARTUP=true (default): run_check() is called before cron loop."""
        with patch.dict(os.environ, {"RUN_ON_STARTUP": "true"}):
            # Re-evaluate the module-level constant
            import monitor
            with patch.object(monitor, "RUN_ON_STARTUP", True):
                with pytest.raises(InterruptedError):
                    monitor.main()
                mock_run_check.assert_called()

    @patch("monitor.run_check")
    @patch("monitor.time.sleep", side_effect=InterruptedError)
    def test_run_check_not_called_on_startup_when_disabled(self, mock_sleep, mock_run_check):
        """RUN_ON_STARTUP=false: run_check() is NOT called before cron loop."""
        import monitor
        with patch.object(monitor, "RUN_ON_STARTUP", False):
            with pytest.raises(InterruptedError):
                monitor.main()
            mock_run_check.assert_not_called()
