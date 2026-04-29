"""Tests for graceful shutdown on SIGTERM/SIGINT."""

import signal
import subprocess
import sys
import time

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
    """Integration test: process exits cleanly on SIGTERM."""

    def test_sigterm_causes_graceful_exit(self, tmp_path):
        """Start the monitor in a subprocess and send SIGTERM; expect exit code 0."""
        proc = subprocess.Popen(
            [sys.executable, "-c", """
import sys, os
os.environ["RUN_ON_STARTUP"] = "false"
os.environ["CRON_SCHEDULE"] = "0 0 1 1 *"
os.environ["DRY_RUN"] = "true"
os.environ["STATE_DB_PATH"] = sys.argv[1]
os.environ["WEB_PORT"] = "0"
from app.main import main
main()
""", str(tmp_path / "state.db")],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )

        # Wait until the process is ready (logged "Next check at:")
        deadline = time.time() + 10
        stderr_lines = []
        import selectors
        sel = selectors.DefaultSelector()
        sel.register(proc.stderr, selectors.EVENT_READ)
        ready = False
        while time.time() < deadline:
            events = sel.select(timeout=1)
            if events:
                line = proc.stderr.readline().decode()
                if not line:
                    break
                stderr_lines.append(line)
                if "Next check at:" in line:
                    ready = True
                    break
            if proc.poll() is not None:
                break
        sel.close()

        if not ready:
            # Process died or never became ready — collect remaining output
            remaining = proc.stderr.read().decode()
            stderr_lines.append(remaining)
            full_stderr = "".join(stderr_lines)
            proc.kill()
            proc.wait(timeout=5)
            raise AssertionError(
                f"Process never became ready (returncode={proc.returncode}).\n"
                f"stderr:\n{full_stderr}"
            )

        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)

        remaining = proc.stderr.read().decode()
        stderr_lines.append(remaining)
        full_stderr = "".join(stderr_lines)

        assert proc.returncode == 0, f"Expected exit 0, got {proc.returncode}.\nstderr:\n{full_stderr}"
        assert "Shutting down gracefully" in full_stderr
