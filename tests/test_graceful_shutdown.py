"""Tests for graceful shutdown on SIGTERM/SIGINT."""

import signal
import subprocess
import sys
import time

import monitor


class TestSignalHandler:
    """Unit tests: signal handler sets the shutdown flag."""

    def setup_method(self):
        monitor.shutdown_requested = False

    def teardown_method(self):
        monitor.shutdown_requested = False

    def test_handle_signal_sets_flag(self):
        assert monitor.shutdown_requested is False
        monitor._handle_signal(signal.SIGTERM, None)
        assert monitor.shutdown_requested is True

    def test_handle_signal_sigint_sets_flag(self):
        assert monitor.shutdown_requested is False
        monitor._handle_signal(signal.SIGINT, None)
        assert monitor.shutdown_requested is True


class TestGracefulShutdownIntegration:
    """Integration test: process exits cleanly on SIGTERM."""

    def test_sigterm_causes_graceful_exit(self):
        """Start the monitor in a subprocess and send SIGTERM; expect exit code 0."""
        proc = subprocess.Popen(
            [sys.executable, "-c", """
import sys, os
os.environ["RUN_ON_STARTUP"] = "false"
os.environ["CRON_SCHEDULE"] = "* * * * *"
os.environ["DRY_RUN"] = "true"
import monitor
monitor.main()
"""],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Give the process time to start and enter the sleep loop
        time.sleep(2)

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        assert proc.returncode == 0
        stderr = proc.stderr.read().decode()
        assert "Shutting down gracefully" in stderr
