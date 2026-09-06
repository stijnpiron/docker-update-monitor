"""Tests for host-aware notifications (task 05).

Covers:
- AC1: webhook payload rows (update / mismatch / warning + host_status) carry host.
- AC2: email HTML and plain-text include a Host column; rows ordered by (host, stack, image).
- AC3: a host transitioning to unreachable emits exactly one down alert.
- AC4: a recovering host emits a "recovered" alert.
- AC5: a second transition within HOST_REACH_COOLDOWN is coalesced (no dispatch);
       after the window elapses it refires. event_cooldowns is updated on dispatch.
- AC6: first-scan priming — no prior reachable-state means no alert.
- AC7: DRY_RUN / no channels suppresses outbound dispatch but still logs.
"""

import contextlib
import logging
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import config as config_mod
from app import http as http_mod
from app import scanner as scanner_mod
from app import state as state_mod
from app.models import UpdateInfo, RegexMismatch, ScanWarning, HostStatusEvent
from app.notifications import notify_host_status
from app.notifications.webhook import notify as webhook_notify, host_updown as webhook_host_updown
from app.notifications.email import (
    _build_html, _build_plain, _build_mismatch_section_html, _build_warnings_section_html,
)


def cfg_patcher(**overrides):
    """Context manager that patches several config attributes at once."""

    class _Patcher:
        def __enter__(self):
            self._stack = contextlib.ExitStack()
            for key, value in overrides.items():
                self._stack.enter_context(patch.object(config_mod, key, value))
            return self

        def __exit__(self, exc_type, exc, tb):
            return self._stack.__exit__(exc_type, exc, tb)

    return _Patcher()


def _make_update(**kwargs):
    defaults = dict(
        container_name="test-app",
        service_name="app",
        stack="mystack",
        image="nginx",
        current_version="1.0.0",
        new_version="1.1.0",
        update_type="minor",
        host="local",
        status="new",
    )
    defaults.update(kwargs)
    return UpdateInfo(**defaults)


def _make_mismatch(**kwargs):
    defaults = dict(
        container_name="test-app",
        service_name="app",
        stack="mystack",
        image="nginx",
        current_tag="latest",
        pattern=r"^\d+$",
        reason="did not match",
        host="local",
    )
    defaults.update(kwargs)
    return RegexMismatch(**defaults)


def _make_warning(**kwargs):
    defaults = dict(
        container_name="test-app",
        image="nginx",
        level="warning",
        message="fetch failed",
        host="local",
    )
    defaults.update(kwargs)
    return ScanWarning(**defaults)


def _down(host="prod-1", error="ssh: Connection timed out"):
    return HostStatusEvent(host=host, event="down", error=error)


def _recovered(host="prod-1"):
    return HostStatusEvent(host=host, event="recovered")


def _webhook_cfg(**overrides):
    base = dict(
        DRY_RUN=False,
        NOTIFY_CHANNELS=["webhook"],
        NOTIFY_ENDPOINT="http://hook.example.com",
        HOST_REACH_COOLDOWN=timedelta(hours=1),
    )
    base.update(overrides)
    return base


_T0 = datetime(2026, 9, 5, 12, 0, 0, tzinfo=timezone.utc)


# ---------------------------------------------------------------------------
# AC1 — webhook payload rows carry host (update / mismatch / warning / host_status)
# ---------------------------------------------------------------------------

class TestWebhookPayloadHost:
    @patch.object(http_mod, "http_session")
    def test_webhook_payload_includes_host(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        updates = [
            _make_update(container_name="a", host="prod-1", status="new"),
            _make_update(container_name="b", host="prod-2", status="known", image="redis"),
            _make_update(container_name="c", host="prod-3", status="resolved", image="memcached"),
        ]

        with cfg_patcher(**_webhook_cfg(NOTIFY_AUTH_TYPE="", NOTIFY_AUTH_TOKEN="")):
            webhook_notify(updates, mismatches=[_make_mismatch(host="prod-1")],
                           warnings=[_make_warning(host="prod-2")])

        payload = mock_session.post.call_args[1]["json"]
        assert payload["new"][0]["host"] == "prod-1"
        assert payload["known"][0]["host"] == "prod-2"
        assert payload["resolved"][0]["host"] == "prod-3"
        assert payload["regex_mismatches"][0]["host"] == "prod-1"
        assert payload["warnings"][0]["host"] == "prod-2"

    @patch.object(http_mod, "http_session")
    def test_host_status_payload_includes_host(self, mock_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.raise_for_status = MagicMock()
        mock_session.post.return_value = mock_resp

        with cfg_patcher(**_webhook_cfg(NOTIFY_AUTH_TYPE="", NOTIFY_AUTH_TOKEN="")):
            webhook_host_updown(_down(host="prod-9", error="boom"))

        payload = mock_session.post.call_args[1]["json"]
        assert payload["type"] == "host_status"
        assert payload["host"] == "prod-9"
        assert payload["event"] == "down"
        assert payload["error"] == "boom"


# ---------------------------------------------------------------------------
# AC2 — email HTML + plain include a Host column; rows ordered by (host, stack, image)
# ---------------------------------------------------------------------------

class TestEmailHostColumn:
    def test_email_html_has_host_column_and_sort(self):
        # Deliberately out of host order; the builder must sort by host first.
        updates = [
            _make_update(container_name="b", host="beta-host", stack="s1", image="redis"),
            _make_update(container_name="a", host="alpha-host", stack="s9", image="nginx"),
        ]
        html = _build_html(updates)

        assert ">Host<" in html
        assert "alpha-host" in html
        assert "beta-host" in html
        assert html.index("alpha-host") < html.index("beta-host")

    def test_email_html_host_escapes_special_chars(self):
        html = _build_html([_make_update(host="prod <ops>")])
        assert "prod &lt;ops&gt;" in html
        assert "prod <ops>" not in html

    def test_email_plain_has_host(self):
        text = _build_plain([_make_update(host="prod-7", stack="mystack")])
        assert "[prod-7]" in text

    def test_email_mismatch_section_has_host(self):
        html = _build_mismatch_section_html([_make_mismatch(host="mismatch-host")])
        assert ">Host<" in html
        assert "mismatch-host" in html

    def test_email_warnings_section_has_host(self):
        html = _build_warnings_section_html([_make_warning(host="warn-host")])
        assert ">Host<" in html
        assert "warn-host" in html

    def test_email_plain_has_host_for_mismatch_and_warning(self):
        text = _build_plain(
            [_make_update(host="h1")],
            mismatches=[_make_mismatch(host="h2")],
            warnings=[_make_warning(host="h3")],
        )
        assert "[h1]" in text
        assert "[h2]" in text
        assert "[h3]" in text


# ---------------------------------------------------------------------------
# AC3 / AC4 — down and recovered alerts dispatch once over active channels
# ---------------------------------------------------------------------------

class TestHostUpDownDispatch:
    @patch("app.notifications.host_status.webhook_host_updown")
    def test_host_down_alert_emitted_once(self, mock_webhook):
        mock_webhook.return_value = True
        with cfg_patcher(**_webhook_cfg()):
            notify_host_status([_down("prod-1")])
        mock_webhook.assert_called_once_with(_down("prod-1"))

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_host_recovered_alert_emitted(self, mock_webhook):
        mock_webhook.return_value = True
        with cfg_patcher(**_webhook_cfg()):
            notify_host_status([_recovered("prod-1")])
        mock_webhook.assert_called_once_with(_recovered("prod-1"))

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_empty_events_is_noop(self, mock_webhook):
        with cfg_patcher(**_webhook_cfg()):
            notify_host_status([])
        mock_webhook.assert_not_called()


# ---------------------------------------------------------------------------
# AC5 — coalescing via event_cooldowns
# ---------------------------------------------------------------------------

class TestHostAlertCoalescing:
    """Two transitions for the same host within the cooldown window → one dispatch."""

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_host_alert_coalesced_within_cooldown(self, mock_webhook):
        mock_webhook.return_value = True
        with cfg_patcher(**_webhook_cfg()):
            notify_host_status([_down("prod-1")], now=_T0)
            # 10 minutes later — still inside the 1h cooldown window.
            notify_host_status([_down("prod-1")], now=_T0 + timedelta(minutes=10))

        mock_webhook.assert_called_once()
        assert state_mod.get_event_last_fired("host:prod-1") == _T0.isoformat()

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_host_alert_refires_after_cooldown(self, mock_webhook):
        mock_webhook.return_value = True
        with cfg_patcher(**_webhook_cfg()):
            notify_host_status([_down("prod-1")], now=_T0)
            # 2 hours later — cooldown has elapsed, so it fires again.
            notify_host_status([_down("prod-1")], now=_T0 + timedelta(hours=2))

        assert mock_webhook.call_count == 2
        assert state_mod.get_event_last_fired("host:prod-1") == \
            (_T0 + timedelta(hours=2)).isoformat()

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_second_transition_within_window_is_coalesced(self, mock_webhook):
        """AC5: down and recovered share the per-host cooldown key
        (``host:<name>``). A second transition of either kind within the
        cooldown window is suppressed, so a host that goes down then quickly
        recovers emits only the single down alert."""
        mock_webhook.return_value = True
        with cfg_patcher(**_webhook_cfg()):
            notify_host_status([_down("prod-1")], now=_T0)
            notify_host_status([_recovered("prod-1")], now=_T0 + timedelta(minutes=5))

        assert mock_webhook.call_count == 1
        mock_webhook.assert_called_once_with(_down("prod-1"))


# ---------------------------------------------------------------------------
# AC6 — first-scan priming: no prior reachable-state means no alert
# ---------------------------------------------------------------------------

class TestFirstScanPriming:
    def test_no_alert_on_first_scan_without_prior_state(self):
        # A host down on the very first scan has no previous reachable baseline.
        before: dict[str, bool] = {}
        after = {"prod-1": False, "local": True}
        assert scanner_mod._detect_host_events(before, after) == []

    def test_down_alert_after_baseline(self):
        before = {"prod-1": True}
        after = {"prod-1": False}
        scanner_mod._last_host_errors["prod-1"] = "ssh: Connection timed out"
        try:
            events = scanner_mod._detect_host_events(before, after)
        finally:
            scanner_mod._last_host_errors.pop("prod-1", None)
        assert events == [HostStatusEvent(
            host="prod-1", event="down", error="ssh: Connection timed out",
        )]

    def test_recovered_alert_after_baseline(self):
        before = {"prod-1": False}
        after = {"prod-1": True}
        assert scanner_mod._detect_host_events(before, after) == [
            HostStatusEvent(host="prod-1", event="recovered"),
        ]

    def test_unchanged_state_produces_no_event(self):
        assert scanner_mod._detect_host_events({"h": True}, {"h": True}) == []
        assert scanner_mod._detect_host_events({"h": False}, {"h": False}) == []


# ---------------------------------------------------------------------------
# AC7 — DRY_RUN / no channels suppress dispatch but still log
# ---------------------------------------------------------------------------

class TestDryRunAndNoChannels:
    @patch("app.notifications.host_status.webhook_host_updown")
    def test_dry_run_suppresses_host_alert_but_logs(self, mock_webhook, caplog):
        with cfg_patcher(**_webhook_cfg(DRY_RUN=True)), caplog.at_level(logging.INFO):
            notify_host_status([_down("prod-1", error="boom")], now=_T0)

        mock_webhook.assert_not_called()
        assert "prod-1" in caplog.text
        assert "boom" in caplog.text
        # No cooldown was consumed by a dry-run event.
        assert state_mod.get_event_last_fired("host:prod-1") is None

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_no_endpoint_suppresses_dispatch_but_logs(self, mock_webhook, caplog):
        with cfg_patcher(**_webhook_cfg(NOTIFY_ENDPOINT="")), caplog.at_level(logging.INFO):
            notify_host_status([_down("prod-1", error="boom")], now=_T0)

        mock_webhook.assert_not_called()
        assert "prod-1" in caplog.text

    @patch("app.notifications.host_status.webhook_host_updown")
    def test_no_configured_channels_suppresses(self, mock_webhook):
        with cfg_patcher(**_webhook_cfg(NOTIFY_CHANNELS=[])):
            notify_host_status([_down("prod-1")], now=_T0)
        mock_webhook.assert_not_called()
