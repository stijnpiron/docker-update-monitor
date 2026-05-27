"""Tests for graceful shutdown on SIGTERM/SIGINT."""

import logging
import signal
from unittest.mock import patch

import pytest

import app.config as _config
from app import main as main_mod


class TestSignalHandler:
    """Unit tests: signal handler sets the shutdown flag."""

    def setup_method(self):
        main_mod.shutdown_requested = False

    def teardown_method(self):
        main_mod.shutdown_requested = False

    def test_handle_signal_sets_flag(self):
        assert main_mod.shutdown_requested is False
        main_mod._handle_signal(signal.SIGTERM, None)
        assert main_mod.shutdown_requested is True

    def test_handle_signal_sigint_sets_flag(self):
        assert main_mod.shutdown_requested is False
        main_mod._handle_signal(signal.SIGINT, None)
        assert main_mod.shutdown_requested is True


class TestGracefulShutdownIntegration:
    """Integration test: main() exits cleanly when shutdown is requested."""

    def test_sigterm_causes_graceful_exit(self, monkeypatch, caplog):
        """Trigger shutdown during the wait loop; expect exit code 0 and graceful log."""
        monkeypatch.setattr(_config, "RUN_ON_STARTUP", False)
        monkeypatch.setattr(_config, "CRON_SCHEDULE", "0 0 1 1 *")

        def trigger_shutdown(_seconds):
            main_mod.shutdown_requested = True

        with patch("app.main.start_dashboard"), \
             patch("app.main.load_last_check", return_value=None), \
             patch("app.main.time.sleep", side_effect=trigger_shutdown), \
             caplog.at_level(logging.INFO):
            with pytest.raises(SystemExit) as exc_info:
                main_mod.main()

        assert exc_info.value.code == 0
        assert "Shutting down gracefully" in caplog.text
