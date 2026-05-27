"""Tests for the /health endpoint."""

from datetime import datetime, timezone
from unittest.mock import patch

import pytest

from app import health


@pytest.fixture(autouse=True)
def _reset_health_state():
    """Reset health module state between tests."""
    with health._state_lock:
        health._state["last_check"] = None
        health._state["next_check"] = None
        health._state["containers_monitored"] = 0
    yield


class TestHealthResponse:
    """Test the response building logic."""

    def test_returns_200_starting_before_first_check(self):
        status, body = health._build_response()
        assert status == 200
        assert body["status"] == "starting"
        assert "waiting for first scan" in body["note"]

    def test_returns_200_after_check(self):
        now = datetime(2026, 4, 28, 3, 0, 0, tzinfo=timezone.utc)
        next_t = datetime(2026, 5, 5, 3, 0, 0, tzinfo=timezone.utc)
        health.update_state(last_check=now, next_check=next_t, containers_monitored=12)

        status, body = health._build_response()
        assert status == 200
        assert body["status"] == "ok"
        assert body["last_check"] == "2026-04-28T03:00:00Z"
        assert body["next_check"] == "2026-05-05T03:00:00Z"
        assert body["containers_monitored"] == 12
        assert isinstance(body["uptime_seconds"], int)

    def test_update_state_partial(self):
        now = datetime(2026, 4, 28, 3, 0, 0, tzinfo=timezone.utc)
        health.update_state(last_check=now)
        health.update_state(containers_monitored=5)

        with health._state_lock:
            assert health._state["last_check"] == "2026-04-28T03:00:00Z"
            assert health._state["containers_monitored"] == 5

    def test_update_state_warnings_not_mutated_by_caller(self):
        """Verify that modifying the list after update_state doesn't affect internal state."""
        warnings = [{"container_name": "app", "message": "warn"}]
        health.update_state(warnings=warnings)
        # Mutate the original list
        warnings.append({"container_name": "evil", "message": "injected"})

        with health._state_lock:
            # State should not have the injected entry
            assert len(health._state["warnings"]) == 1

    def test_update_state_stores_skipped_containers(self):
        skipped = [{"container_name": "x", "stack": "s", "image": "img", "reason": "no label"}]
        health.update_state(skipped_containers=skipped)

        with health._state_lock:
            assert health._state["skipped_containers"] == skipped

    def test_update_state_releases_lock_before_save_last_check(self):
        """save_last_check must be called outside _state_lock so DB I/O does not stall readers."""
        lock_held_during_save: list[bool] = []

        def fake_save(iso):
            lock_held_during_save.append(health._state_lock.locked())

        now = datetime(2026, 4, 28, 3, 0, 0, tzinfo=timezone.utc)
        with patch("app.health.save_last_check", side_effect=fake_save):
            health.update_state(last_check=now)

        assert lock_held_during_save == [False], (
            "save_last_check must be called with _state_lock released"
        )

    def test_update_state_does_not_persist_when_last_check_absent(self):
        """save_last_check is only invoked when last_check is provided."""
        with patch("app.health.save_last_check") as mock_save:
            health.update_state(containers_monitored=3)
        mock_save.assert_not_called()
