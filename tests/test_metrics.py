"""Tests for Prometheus metrics module and /metrics endpoint."""

import pytest
from unittest.mock import patch, MagicMock

import app.metrics as metrics_mod
from app.metrics import (
    containers_monitored,
    updates_available,
    host_reachable,
    check_duration_seconds,
    check_errors_total,
    last_check_timestamp_seconds,
    notifications_attempted_total,
    notifications_sent_total,
    update_after_scan,
)
from app.models import UpdateInfo


@pytest.fixture(autouse=True)
def reset_seen_types():
    """Reset the seen-label tracking sets and zero all labelled gauges between tests."""
    metrics_mod._seen_update_labels = set()
    metrics_mod._seen_hosts = set()
    for child in list(metrics_mod.updates_available._metrics.values()):
        child.set(0)
    for child in list(metrics_mod.host_reachable._metrics.values()):
        child.set(0)
    yield
    metrics_mod._seen_update_labels = set()
    metrics_mod._seen_hosts = set()


@pytest.fixture
def client():
    """Flask test client with the full app."""
    from app.dashboard import create_app
    app = create_app()
    app.config["TESTING"] = True
    with app.test_client() as c:
        yield c


class TestUpdateAfterScanGauges:
    """update_after_scan() sets gauge values correctly."""

    def test_sets_containers_monitored(self):
        update_after_scan(monitored=7, updates=[], duration_seconds=0.0, last_check_ts=0.0)
        assert containers_monitored._value.get() == 7.0

    def test_sets_check_duration(self):
        update_after_scan(monitored=0, updates=[], duration_seconds=14.2, last_check_ts=0.0)
        assert abs(check_duration_seconds._value.get() - 14.2) < 1e-6

    def test_sets_last_check_timestamp(self):
        update_after_scan(monitored=0, updates=[], duration_seconds=0.0, last_check_ts=1714300800.0)
        assert last_check_timestamp_seconds._value.get() == 1714300800.0

    def test_counts_updates_by_type_from_dicts(self):
        updates = [
            {"status": "new", "update_type": "patch"},
            {"status": "known", "update_type": "patch"},
            {"status": "new", "update_type": "minor"},
        ]
        update_after_scan(monitored=3, updates=updates, duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="patch", host="local")._value.get() == 2.0
        assert updates_available.labels(type="minor", host="local")._value.get() == 1.0

    def test_counts_updates_by_type_from_update_info_objects(self):
        u1 = UpdateInfo("c1", "s", "st", "img", "1.0", "2.0", "major", status="new")
        u2 = UpdateInfo("c2", "s", "st", "img", "1.0", "1.1", "minor", status="known")
        update_after_scan(monitored=2, updates=[u1, u2], duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="major", host="local")._value.get() == 1.0
        assert updates_available.labels(type="minor", host="local")._value.get() == 1.0

    def test_resolved_updates_excluded_from_count(self):
        updates = [{"status": "resolved", "update_type": "major"}]
        update_after_scan(monitored=1, updates=updates, duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="major", host="local")._value.get() == 0.0

    def test_zeroes_out_labels_no_longer_present(self):
        updates1 = [
            {"status": "new", "update_type": "patch"},
            {"status": "known", "update_type": "patch"},
        ]
        update_after_scan(monitored=2, updates=updates1, duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="patch", host="local")._value.get() == 2.0

        update_after_scan(monitored=0, updates=[], duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="patch", host="local")._value.get() == 0.0

    def test_multiple_update_types(self):
        updates = [
            {"status": "new", "update_type": "patch"},
            {"status": "new", "update_type": "minor"},
            {"status": "new", "update_type": "minor"},
            {"status": "new", "update_type": "major"},
            {"status": "new", "update_type": "digest"},
        ]
        update_after_scan(monitored=5, updates=updates, duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="patch", host="local")._value.get() == 1.0
        assert updates_available.labels(type="minor", host="local")._value.get() == 2.0
        assert updates_available.labels(type="major", host="local")._value.get() == 1.0
        assert updates_available.labels(type="digest", host="local")._value.get() == 1.0


class TestHostLabels:
    """Host-aware metrics: dum_host_reachable + host-labelled dum_updates_available."""

    # AC1 — dum_host_reachable gauge is set per host with 0/1 values.
    def test_host_reachable_gauge_set_per_host(self):
        hs = [
            {"host": "local", "reachable": True, "error": None},
            {"host": "prod-1", "reachable": False, "error": "ssh: timeout"},
        ]
        update_after_scan(monitored=0, updates=[], duration_seconds=0.0, last_check_ts=0.0, host_status=hs)
        assert host_reachable.labels(host="local")._value.get() == 1.0
        assert host_reachable.labels(host="prod-1")._value.get() == 0.0

    # AC2 — per-host pending counts labelled by (host, type).
    def test_updates_available_labelled_by_host_and_type(self):
        updates = [
            {"status": "new", "update_type": "patch", "host": "local"},
            {"status": "new", "update_type": "patch", "host": "local"},
            {"status": "new", "update_type": "minor", "host": "prod-1"},
            {"status": "resolved", "update_type": "major", "host": "prod-1"},  # excluded
        ]
        update_after_scan(monitored=4, updates=updates, duration_seconds=0.0, last_check_ts=0.0)
        assert updates_available.labels(type="patch", host="local")._value.get() == 2.0
        assert updates_available.labels(type="minor", host="prod-1")._value.get() == 1.0
        # Resolved is not counted, so (major, prod-1) never gets a live label.
        assert updates_available.labels(type="major", host="prod-1")._value.get() == 0.0

    # AC3 — a host present in one scan and removed in the next is zeroed/removal.
    def test_stale_host_labels_zeroed(self):
        scan1 = [
            {"status": "new", "update_type": "patch", "host": "local"},
            {"status": "new", "update_type": "patch", "host": "prod-1"},
        ]
        hs1 = [
            {"host": "local", "reachable": True, "error": None},
            {"host": "prod-1", "reachable": True, "error": None},
        ]
        update_after_scan(monitored=2, updates=scan1, duration_seconds=0.0, last_check_ts=0.0, host_status=hs1)
        assert updates_available.labels(type="patch", host="prod-1")._value.get() == 1.0
        assert host_reachable.labels(host="prod-1")._value.get() == 1.0

        # Next scan: prod-1 dropped from config → its DB/host_status rows filtered
        # out by the caller, so it is absent from both inputs.
        scan2 = [{"status": "new", "update_type": "patch", "host": "local"}]
        hs2 = [{"host": "local", "reachable": True, "error": None}]
        update_after_scan(monitored=1, updates=scan2, duration_seconds=0.0, last_check_ts=0.0, host_status=hs2)
        assert updates_available.labels(type="patch", host="prod-1")._value.get() == 0.0
        assert host_reachable.labels(host="prod-1")._value.get() == 0.0
        # local unaffected
        assert updates_available.labels(type="patch", host="local")._value.get() == 1.0
        assert host_reachable.labels(host="local")._value.get() == 1.0

    # AC4 — global aggregate metrics are unchanged by the host feature.
    def test_global_metrics_unchanged(self):
        update_after_scan(
            monitored=9,
            updates=[{"status": "new", "update_type": "patch", "host": "local"}],
            duration_seconds=3.5,
            last_check_ts=1700000000.0,
        )
        assert containers_monitored._value.get() == 9.0
        assert abs(check_duration_seconds._value.get() - 3.5) < 1e-6
        assert last_check_timestamp_seconds._value.get() == 1700000000.0

    # AC5 — single local host: only host="local" label is emitted.
    def test_single_host_backwards_compatible_labels(self):
        updates = [
            {"status": "new", "update_type": "patch", "host": "local"},
            {"status": "new", "update_type": "minor", "host": "local"},
        ]
        hs = [{"host": "local", "reachable": True, "error": None}]
        update_after_scan(monitored=2, updates=updates, duration_seconds=0.0, last_check_ts=0.0, host_status=hs)

        # Only non-zero series count as "active" — zeroed/stale series remain in
        # the registry with a 0 value (that is the intended zero-out behavior).
        ua_active = {k: m._value.get() for k, m in updates_available._metrics.items()
                     if m._value.get() > 0}                 # {(type, host): count}
        hr_active = {k[0]: m._value.get() for k, m in host_reachable._metrics.items()
                     if m._value.get() > 0}                 # {host: 1/0}
        assert all(h == "local" for _t, h in ua_active)
        assert all(h == "local" for h in hr_active)
        # The type dimension is still present (superset of the previous surface).
        assert {"patch", "minor"} <= {t for t, _h in ua_active}
        assert hr_active.get("local") == 1


class TestCheckErrorsCounter:
    """check_errors_total increments correctly."""

    def test_increments_on_explicit_call(self):
        before = check_errors_total._value.get()
        check_errors_total.inc()
        assert check_errors_total._value.get() == before + 1.0

    def test_increments_by_n(self):
        before = check_errors_total._value.get()
        check_errors_total.inc(3)
        assert check_errors_total._value.get() == before + 3.0

    def test_scanner_increments_on_docker_failure(self):
        from docker.errors import DockerException
        before = check_errors_total._value.get()
        with patch("app.scanner.docker.from_env", side_effect=DockerException("fail")):
            from app.scanner import run_check
            run_check()
        assert check_errors_total._value.get() == before + 1.0

    def test_scanner_increments_for_warnings(self):
        """Each ScanWarning produced during a scan increments the error counter."""
        from app.scanner import run_check

        mock_client = MagicMock()
        mock_container = MagicMock()
        mock_container.name = "broken"
        mock_container.labels = {
            "docker-update-monitor.tag-regex": "(",  # invalid regex → warning
        }
        mock_container.image.tags = ["nginx:latest"]
        mock_container.attrs = {"Config": {"Image": "nginx:latest"}}
        mock_client.containers.list.return_value = [mock_container]

        before = check_errors_total._value.get()
        with patch("app.scanner.docker.from_env", return_value=mock_client), \
             patch("app.scanner.get_dockerhub_token", return_value=None):
            run_check()
        assert check_errors_total._value.get() > before


class TestNotificationsSentCounter:
    """notifications_sent_total increments only on successful delivery."""

    def test_increments_webhook_on_success(self):
        before_a = notifications_attempted_total.labels(channel="webhook")._value.get()
        before_s = notifications_sent_total.labels(channel="webhook")._value.get()
        with patch("app.notifications.webhook_notify", return_value=True), \
             patch("app.config.NOTIFY_CHANNELS", ["webhook"]):
            from app.notifications import dispatch
            u = UpdateInfo("c", "s", "st", "img", "1.0", "2.0", "major", status="new")
            dispatch([u])
        assert notifications_attempted_total.labels(channel="webhook")._value.get() == before_a + 1.0
        assert notifications_sent_total.labels(channel="webhook")._value.get() == before_s + 1.0

    def test_increments_email_on_success(self):
        before_a = notifications_attempted_total.labels(channel="email")._value.get()
        before_s = notifications_sent_total.labels(channel="email")._value.get()
        with patch("app.notifications.email_notify", return_value=True), \
             patch("app.config.NOTIFY_CHANNELS", ["email"]):
            from app.notifications import dispatch
            u = UpdateInfo("c", "s", "st", "img", "1.0", "2.0", "major", status="new")
            dispatch([u])
        assert notifications_attempted_total.labels(channel="email")._value.get() == before_a + 1.0
        assert notifications_sent_total.labels(channel="email")._value.get() == before_s + 1.0

    def test_webhook_failure_increments_attempted_only(self):
        before_a = notifications_attempted_total.labels(channel="webhook")._value.get()
        before_s = notifications_sent_total.labels(channel="webhook")._value.get()
        with patch("app.notifications.webhook_notify", return_value=False), \
             patch("app.config.NOTIFY_CHANNELS", ["webhook"]):
            from app.notifications import dispatch
            u = UpdateInfo("c", "s", "st", "img", "1.0", "2.0", "major", status="new")
            dispatch([u])
        assert notifications_attempted_total.labels(channel="webhook")._value.get() == before_a + 1.0
        assert notifications_sent_total.labels(channel="webhook")._value.get() == before_s

    def test_email_failure_increments_attempted_only(self):
        before_a = notifications_attempted_total.labels(channel="email")._value.get()
        before_s = notifications_sent_total.labels(channel="email")._value.get()
        with patch("app.notifications.email_notify", return_value=False), \
             patch("app.config.NOTIFY_CHANNELS", ["email"]):
            from app.notifications import dispatch
            u = UpdateInfo("c", "s", "st", "img", "1.0", "2.0", "major", status="new")
            dispatch([u])
        assert notifications_attempted_total.labels(channel="email")._value.get() == before_a + 1.0
        assert notifications_sent_total.labels(channel="email")._value.get() == before_s

    def test_skipped_notifier_does_not_increment_either_counter(self):
        """When the notifier returns None (no attempt made), neither counter moves."""
        before_a = notifications_attempted_total.labels(channel="webhook")._value.get()
        before_s = notifications_sent_total.labels(channel="webhook")._value.get()
        with patch("app.notifications.webhook_notify", return_value=None), \
             patch("app.config.NOTIFY_CHANNELS", ["webhook"]):
            from app.notifications import dispatch
            u = UpdateInfo("c", "s", "st", "img", "1.0", "2.0", "major", status="new")
            dispatch([u])
        assert notifications_attempted_total.labels(channel="webhook")._value.get() == before_a
        assert notifications_sent_total.labels(channel="webhook")._value.get() == before_s

    def test_no_increment_when_nothing_to_notify(self):
        before_aw = notifications_attempted_total.labels(channel="webhook")._value.get()
        before_sw = notifications_sent_total.labels(channel="webhook")._value.get()
        before_ae = notifications_attempted_total.labels(channel="email")._value.get()
        before_se = notifications_sent_total.labels(channel="email")._value.get()
        with patch("app.config.NOTIFY_CHANNELS", ["webhook", "email"]):
            from app.notifications import dispatch
            dispatch([])
        assert notifications_attempted_total.labels(channel="webhook")._value.get() == before_aw
        assert notifications_sent_total.labels(channel="webhook")._value.get() == before_sw
        assert notifications_attempted_total.labels(channel="email")._value.get() == before_ae
        assert notifications_sent_total.labels(channel="email")._value.get() == before_se


class TestMetricsEndpoint:
    """GET /metrics returns valid Prometheus text format."""

    def test_returns_200(self, client):
        resp = client.get("/metrics")
        assert resp.status_code == 200

    def test_content_type_is_prometheus(self, client):
        resp = client.get("/metrics")
        assert "text/plain" in resp.content_type

    def test_contains_dum_containers_monitored(self, client):
        update_after_scan(monitored=3, updates=[], duration_seconds=1.0, last_check_ts=100.0)
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_containers_monitored" in body

    def test_contains_dum_updates_available(self, client):
        updates = [{"status": "new", "update_type": "minor"}]
        update_after_scan(monitored=1, updates=updates, duration_seconds=1.0, last_check_ts=100.0)
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_updates_available" in body
        assert 'type="minor"' in body

    def test_contains_dum_check_duration_seconds(self, client):
        update_after_scan(monitored=0, updates=[], duration_seconds=5.5, last_check_ts=100.0)
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_check_duration_seconds" in body

    def test_contains_dum_check_errors_total(self, client):
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_check_errors_total" in body

    def test_contains_dum_last_check_timestamp_seconds(self, client):
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_last_check_timestamp_seconds" in body

    def test_contains_dum_notifications_sent_total(self, client):
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_notifications_sent_total" in body

    def test_contains_dum_notifications_attempted_total(self, client):
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_notifications_attempted_total" in body

    def test_metric_values_reflect_last_scan(self, client):
        import re
        updates = [
            {"status": "new", "update_type": "patch"},
            {"status": "new", "update_type": "patch"},
            {"status": "new", "update_type": "major"},
        ]
        update_after_scan(monitored=12, updates=updates, duration_seconds=14.2, last_check_ts=1714300800.0)
        resp = client.get("/metrics")
        body = resp.data.decode()
        assert "dum_containers_monitored 12.0" in body
        m = re.search(r"^dum_last_check_timestamp_seconds (\S+)", body, re.MULTILINE)
        assert m is not None
        assert abs(float(m.group(1)) - 1714300800.0) < 1.0
