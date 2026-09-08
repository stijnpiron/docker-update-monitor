"""Multi-host scanner tests (task 04).

Covers the per-host scan loop in ``app.scanner.run_check``:
- local-only backward compatibility (AC1)
- multi-host scanning with per-host scoping (AC2, AC3)
- unreachable hosts are skipped, recorded, and non-fatal (AC2, AC5)
- cross-host dedup key includes host (AC4)
- clients always closed, including the error path (AC6)
- single notification dispatch per scan (AC7)
- host_status surfaced through app/health.py state
"""

import logging
from datetime import datetime as _real_datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from docker.errors import DockerException

from app import config as config_mod
from app import health as health_mod
from app import scanner as scanner_mod
from app import state as state_mod
from app.metrics import (
    check_duration_seconds,
    host_reachable,
    last_check_timestamp_seconds,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LOCAL_ONLY = [("local", None)]
_TWO_HOSTS = [("local", None), ("remote-a", "ssh://user@host1/tcp")]
_THREE_HOSTS = [
    ("local", None),
    ("host-b", "ssh://user@hostb/tcp"),
    ("host-c", "ssh://user@hostc/tcp"),
]


def _make_container(name, image_tag, labels=None):
    c = MagicMock()
    c.name = name
    c.labels = labels or {}
    c.image.tags = [image_tag]
    c.image.attrs = {}
    c.attrs = {"Config": {"Image": image_tag}}
    return c


def _monitored_container(name, image_tag, pattern=r"^(\d+)\.(\d+)$"):
    return _make_container(
        name, image_tag, {f"{config_mod.LABEL_PREFIX}.tag-regex": pattern}
    )


def _container_client(name, containers, all_containers=None):
    """A mock client whose containers.list() returns *containers* (running)
    and containers.list(all=True) returns *all_containers* (or the running
    list when not given)."""
    c = MagicMock(name=f"client-{name}")
    ac = all_containers if all_containers is not None else list(containers)

    def _list(all=False):
        return ac if all else list(containers)

    c.containers.list.side_effect = _list
    return c


class _CloseSpy:
    """Wraps a MagicMock; test-side counter for close() calls."""

    def __init__(self, name):
        self.name = name
        self.inner = MagicMock(name=f"inner-{name}")
        self.close_count = 0

    def __getattr__(self, item):
        return getattr(self.inner, item)

    def close(self):
        self.close_count += 1
        self.inner.close()


@pytest.fixture(autouse=True)
def _reset_scanner_module():
    """Reset module-level reachability tracking between tests."""
    yield
    scanner_mod._last_host_reachable.clear()
    scanner_mod._last_host_errors.clear()


def _patch_hosts(hosts):
    return patch.object(config_mod, "DOCKER_HOSTS", hosts)


# ---------------------------------------------------------------------------
# AC1 — single local host, backward compatible
# ---------------------------------------------------------------------------

class TestSingleLocalHost:
    """DOCKER_HOSTS = [('local', None)] — the pre-feature behavior."""

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_single_local_host_tags_local(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """All artifacts from the local daemon carry host='local'."""
        container = _monitored_container("web", "myapp:1.1")
        mock_docker.from_env.return_value = _container_client("local", [container])

        with _patch_hosts(_LOCAL_ONLY), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        # DB row
        rows = [r for r in state_mod.get_all_updates() if r["container_name"] == "web"]
        assert len(rows) == 1
        assert rows[0]["host"] == "local"

        # notify payload
        mock_notify.assert_called_once()
        actionable = mock_notify.call_args.args[0]
        assert len(actionable) == 1
        assert actionable[0].host == "local"

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_local_host_status_recorded(
        self, mock_docker, mock_token, mock_notify
    ):
        """After a successful single-local-host scan, host_status has one
        reachable row for 'local'."""
        mock_docker.from_env.return_value = _container_client("local", [])

        with _patch_hosts(_LOCAL_ONLY), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        status = state_mod.get_host_status()
        assert len(status) == 1
        assert status[0]["host"] == "local"
        assert status[0]["reachable"] == 1
        assert status[0]["error"] is None
        assert scanner_mod._last_host_reachable == {"local": True}

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_local_unreachable_recorded_in_host_status(
        self, mock_docker, mock_token, mock_notify, caplog
    ):
        """F2a: a single unreachable local host is recorded as unreachable in
        host_status (no special-case early return) and the all-down warning is
        logged. The all-down cycle still reaches notify() with an empty payload
        (QA D5 — no early return before the metrics/health update); dispatch
        no-ops internally on an empty payload, so no channel is contacted."""
        mock_docker.from_env.side_effect = DockerException("cannot connect")

        with _patch_hosts(_LOCAL_ONLY), \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.WARNING):
            scanner_mod.run_check()

        assert "Unreachable" in caplog.text
        assert "No hosts were reachable in this scan cycle" in caplog.text
        # notify() is reached with an empty payload (not skipped), which the
        # real dispatch no-ops on.
        mock_notify.assert_called_once_with([], mismatches=[], warnings=[])

        status = state_mod.get_host_status()
        assert len(status) == 1
        assert status[0]["host"] == "local"
        assert status[0]["reachable"] == 0
        assert status[0]["error"] is not None
        assert "cannot connect" in status[0]["error"]


# ---------------------------------------------------------------------------
# AC2 / AC5 — multi-host: unreachable hosts skipped, host_status written
# ---------------------------------------------------------------------------

class TestMultiHostReachability:

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_two_hosts_both_scanned(
        self, mock_docker, mock_token, mock_notify
    ):
        """Both host clients are invoked; both appear reachable in
        host_status."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.return_value = _container_client("remote", [])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        assert mock_docker.from_env.call_count == 1
        assert mock_docker.DockerClient.call_count == 1

        status = {s["host"]: s for s in state_mod.get_host_status()}
        assert set(status) == {"local", "remote-a"}
        assert status["local"]["reachable"] == 1
        assert status["remote-a"]["reachable"] == 1

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_unreachable_host_skipped_others_complete(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify, caplog
    ):
        """remote-a raises during its scan; local is still scanned, its
        updates persisted and dispatched, and remote-a recorded
        unreachable."""
        container = _monitored_container("api", "myapi:1.1")
        mock_docker.from_env.return_value = _container_client("local", [container])

        remote = MagicMock(name="client-remote")
        remote.containers.list.side_effect = DockerException("SSH channel closed")
        mock_docker.DockerClient.return_value = remote

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.WARNING):
            scanner_mod.run_check()

        assert "Unreachable" in caplog.text
        assert "remote-a" in caplog.text

        rows = [r for r in state_mod.get_all_updates() if r["host"] == "local"]
        assert rows, "expected at least one local update row"
        assert not [r for r in state_mod.get_all_updates() if r["host"] == "remote-a"]

        status = {s["host"]: s for s in state_mod.get_host_status()}
        assert status["local"]["reachable"] == 1
        assert status["remote-a"]["reachable"] == 0
        assert status["remote-a"]["error"]

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_connection_failure_records_unreachable(
        self, mock_docker, mock_token, mock_notify, caplog
    ):
        """docker.DockerClient() raises on connect → the host is recorded
        unreachable and the local scan still completes."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.side_effect = DockerException("SSH refused")

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.WARNING):
            scanner_mod.run_check()

        status = {s["host"]: s for s in state_mod.get_host_status()}
        assert status["local"]["reachable"] == 1
        assert status["remote-a"]["reachable"] == 0

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_unreachable_host_recorded_in_host_status(
        self, mock_docker, mock_token, mock_notify
    ):
        """AC5: every configured host gets a host_status row on every scan,
        reachable or not, and the in-memory transition map is updated."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.side_effect = DockerException("host down")

        with _patch_hosts(_THREE_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        status = {s["host"]: s for s in state_mod.get_host_status()}
        assert set(status) == {"local", "host-b", "host-c"}
        assert status["local"]["reachable"] == 1
        assert status["host-b"]["reachable"] == 0
        assert status["host-c"]["reachable"] == 0
        assert scanner_mod._last_host_reachable == {
            "local": True, "host-b": False, "host-c": False
        }


# ---------------------------------------------------------------------------
# D5 — all hosts down: metrics + "Last scan" must still advance
# ---------------------------------------------------------------------------

class TestAllHostsDownStillUpdatesMetrics:
    """QA D5: when every host in a scan cycle is unreachable, run_check() used
    to early-return before the metrics/health update, leaving
    dum_host_reachable at its previous (healthy) value, the duration/last-check
    gauges stale, and the dashboard's "Last scan" timestamp frozen on exactly
    the cycle an operator most needs them fresh. A Prometheus alert on
    dum_host_reachable == 0 would never fire for an all-down scan. The
    host_status rows + down alerts already write before that point; only the
    metrics/health refresh was skipped."""

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_all_down_second_scan_refreshes_metrics_and_last_scan(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """Healthy first scan establishes the baseline gauges; a fully-down
        second scan must still refresh dum_host_reachable (0), the duration
        gauge, last_check_timestamp_seconds, and app.health's last_check."""
        container = _monitored_container("web", "myapp:1.1")
        healthy_local = _container_client("local", [container])
        healthy_remote = _container_client("remote", [])

        # -- scan 1: both hosts up (baseline). --
        mock_docker.from_env.return_value = healthy_local
        mock_docker.DockerClient.return_value = healthy_remote
        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        assert host_reachable.labels(host="local")._value.get() == 1.0
        assert host_reachable.labels(host="remote-a")._value.get() == 1.0
        first_last_check = last_check_timestamp_seconds._value.get()
        first_duration = check_duration_seconds._value.get()
        first_health_last_check = health_mod._state["last_check"]
        assert first_health_last_check is not None

        # -- scan 2: both hosts unreachable (all-down cycle). --
        mock_docker.from_env.side_effect = DockerException("local daemon down")
        mock_docker.DockerClient.side_effect = DockerException("ssh refused")
        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        # Reachability gauges flip to 0 (the D5 regression: they stayed 1).
        assert host_reachable.labels(host="local")._value.get() == 0.0
        assert host_reachable.labels(host="remote-a")._value.get() == 0.0

        # last_check timestamp advances past the healthy scan.
        second_last_check = last_check_timestamp_seconds._value.get()
        assert second_last_check >= first_last_check

        # Duration is re-measured for this cycle, not left at the baseline.
        assert check_duration_seconds._value.get() > 0.0

        # The dashboard "Last scan" timestamp advances (previously frozen).
        second_health_last_check = health_mod._state["last_check"]
        assert second_health_last_check is not None
        assert second_health_last_check != first_health_last_check

        # The all-down cycle reaches notify() with an empty payload (dispatch
        # no-ops internally) — no update is (re-)dispatched for the outage.
        assert mock_notify.call_count == 2
        mock_notify.assert_called_with([], mismatches=[], warnings=[])


# ---------------------------------------------------------------------------
# F1 — foreign exception type must not abort other hosts (QA gap #1)
# ---------------------------------------------------------------------------

class TestForeignExceptionIsolation:
    """A non-DockerException exception raised inside _scan_host() must
    record the failing host as unreachable and leave other hosts' scans
    completely unaffected (QA finding F1, test gap #1)."""

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_foreign_exception_on_one_host_does_not_abort_others(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify, caplog
    ):
        """remote-a's containers.list() raises a bare RuntimeError (not a
        DockerException or RequestException). local must still be scanned
        and persisted; remote-a must be recorded unreachable with an
        error message prefixed 'scan error:'."""
        local_container = _monitored_container("web", "myapp:1.1")
        mock_docker.from_env.return_value = _container_client("local", [local_container])

        remote = MagicMock(name="client-remote")
        remote.containers.list.side_effect = RuntimeError("simulated SDK internal error")
        mock_docker.DockerClient.return_value = remote

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.ERROR):
            scanner_mod.run_check()  # must not raise

        # local was scanned and its update row written
        local_rows = [r for r in state_mod.get_all_updates() if r["host"] == "local"]
        assert local_rows, "expected a local update row"

        # remote-a recorded unreachable with a 'scan error' message
        status = {s["host"]: s for s in state_mod.get_host_status()}
        assert status["local"]["reachable"] == 1
        assert status["remote-a"]["reachable"] == 0
        assert status["remote-a"]["error"] is not None
        assert "scan error" in status["remote-a"]["error"]
        assert "simulated SDK internal error" in status["remote-a"]["error"]

        # The error was logged with a traceback (exc_info=True)
        assert "scan failed with unexpected error" in caplog.text


# ---------------------------------------------------------------------------
# AC3 — per-host in-memory state scoping (same container name, two hosts)
# ---------------------------------------------------------------------------

class TestPerHostStateIsolation:

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_same_container_name_across_hosts_isolated(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """'nginx' runs on both hosts. It is removed from remote-a between
        scans: remote-a's row is cleaned up while local's row survives."""
        nginx_local = _monitored_container("nginx", "nginx:1.1")
        nginx_remote = _monitored_container("nginx", "nginx:1.1")

        # --- scan 1: both hosts run nginx ---
        mock_docker.from_env.return_value = _container_client("local", [nginx_local])
        mock_docker.DockerClient.return_value = _container_client("remote", [nginx_remote])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        rows = state_mod.get_all_updates()
        assert sum(1 for r in rows if r["host"] == "local" and r["container_name"] == "nginx") == 1
        assert sum(1 for r in rows if r["host"] == "remote-a" and r["container_name"] == "nginx") == 1

        # --- scan 2: nginx removed from remote-a's daemon (and from local's
        # all-containers list too is NOT the case — local still runs it) ---
        mock_docker.from_env.return_value = _container_client("local", [nginx_local])
        mock_docker.DockerClient.return_value = _container_client("remote", [])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        active = state_mod.get_active_updates()
        local_rows = [r for r in active if r["host"] == "local" and r["container_name"] == "nginx"]
        remote_rows = [r for r in active if r["host"] == "remote-a" and r["container_name"] == "nginx"]
        assert local_rows, "local nginx row must survive"
        assert not remote_rows, "remote-a nginx row must be cleaned up"


# ---------------------------------------------------------------------------
# AC4 — cross-host dedup key includes host
# ---------------------------------------------------------------------------

class TestCrossHostDedup:

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_cross_host_dedup_key(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """The same image:version running on two hosts produces two entries
        (one per host); the dedup key must include the host."""
        c_local = _monitored_container("app", "myapp:1.1")
        c_remote = _monitored_container("app", "myapp:1.1")
        mock_docker.from_env.return_value = _container_client("local", [c_local])
        mock_docker.DockerClient.return_value = _container_client("remote", [c_remote])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        actionable = mock_notify.call_args.args[0]
        myapp = [u for u in actionable if u.image == "myapp"]
        assert len(myapp) == 2, (
            f"expected 2 entries (one per host), got {[(u.host, u.container_name) for u in myapp]}"
        )
        assert {u.host for u in myapp} == {"local", "remote-a"}

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_single_host_single_entry(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """The same image on only one host produces exactly one entry."""
        c_local = _monitored_container("app", "myapp:1.1")
        c_remote = _monitored_container("other", "otherapp:1.1")
        mock_docker.from_env.return_value = _container_client("local", [c_local])
        mock_docker.DockerClient.return_value = _container_client("remote", [c_remote])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        actionable = mock_notify.call_args.args[0]
        myapp = [u for u in actionable if u.image == "myapp"]
        assert len(myapp) == 1
        assert myapp[0].host == "local"


# ---------------------------------------------------------------------------
# AC6 — clients always closed, including the error path
# ---------------------------------------------------------------------------

class TestClientCleanup:

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_client_closed_per_host(
        self, mock_docker, mock_token, mock_notify
    ):
        """Both clients are closed exactly once after a successful scan."""
        local_spy = _CloseSpy("local")
        remote_spy = _CloseSpy("remote")
        mock_docker.from_env.return_value = local_spy
        mock_docker.DockerClient.return_value = remote_spy

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        assert local_spy.close_count == 1, "local client must be closed once"
        assert remote_spy.close_count == 1, "remote client must be closed once"

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_client_closed_on_error_path(
        self, mock_docker, mock_token, mock_notify
    ):
        """close() is called on a client whose scan raised mid-way."""
        local_spy = _CloseSpy("local")
        remote_spy = _CloseSpy("remote")
        remote_spy.inner.containers.list.side_effect = DockerException("boom")
        mock_docker.from_env.return_value = local_spy
        mock_docker.DockerClient.return_value = remote_spy

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        assert local_spy.close_count == 1
        assert remote_spy.close_count == 1, "client must be closed even when its scan raises"


# ---------------------------------------------------------------------------
# AC7 — single notification dispatch per scan
# ---------------------------------------------------------------------------

class TestSingleNotificationDispatch:

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_single_notification_dispatch_across_hosts(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """Two hosts updating → exactly one notify() call carrying the
        merged, host-tagged list."""
        c_local = _monitored_container("svc-local", "imgA:1.1")
        c_remote = _monitored_container("svc-remote", "imgB:1.1")
        mock_docker.from_env.return_value = _container_client("local", [c_local])
        mock_docker.DockerClient.return_value = _container_client("remote", [c_remote])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        assert mock_notify.call_count == 1
        actionable = mock_notify.call_args.args[0]
        assert len(actionable) == 2
        assert {u.host for u in actionable} == {"local", "remote-a"}

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=[])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_no_updates_no_notification_payload(
        self, mock_docker, mock_token, mock_fetch_tags, mock_notify
    ):
        """All hosts scanned, nothing to report → notify is called once with
        an empty actionable list."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.return_value = _container_client("remote", [])

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        assert mock_notify.call_count == 1
        assert mock_notify.call_args.args[0] == []


# ---------------------------------------------------------------------------
# host_status surfaced through health.py state (task 06 readiness)
# ---------------------------------------------------------------------------

class TestHostStatusInHealthState:

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_host_status_in_health_state(
        self, mock_docker, mock_token, mock_notify
    ):
        """After a scan, health._state['host_status'] holds the snapshots
        for every configured host."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.side_effect = DockerException("offline")

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        with health_mod._state_lock:
            hs = list(health_mod._state.get("host_status", []))

        by_host = {h["host"]: h for h in hs}
        assert set(by_host) == {"local", "remote-a"}
        assert by_host["local"]["reachable"] == 1
        assert by_host["remote-a"]["reachable"] == 0


# ---------------------------------------------------------------------------
# D2 — "unreachable since" uses the transition time, not the latest scan
# ---------------------------------------------------------------------------

class TestDownSinceStreak:
    """QA D2: ``host_status.down_since`` records the *start* of the current
    unreachable streak (the first scan after the previous known-good state),
    not the latest scan. ``checked_at`` keeps advancing each scan, but the
    "since" value shown to the operator must remain pinned to when the host
    actually went down.
    """

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    @patch.object(scanner_mod, "datetime")
    def test_down_since_kept_across_consecutive_down_scans(
        self, mock_dt, mock_docker, mock_token, mock_notify
    ):
        """Three scans (up → down → down): ``down_since`` is the 2nd scan's
        time on both down scans, while ``checked_at`` advances each scan."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.side_effect = DockerException("SSH refused")

        t1 = _real_datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = _real_datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)
        t3 = _real_datetime(2026, 1, 1, 12, 0, 0, tzinfo=timezone.utc)

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            # Scan 1 — remote up (baseline).
            mock_dt.now.return_value = t1
            mock_docker.DockerClient.side_effect = None
            mock_docker.DockerClient.return_value = _container_client("remote-a", [])
            scanner_mod.run_check()

            # Scan 2 — remote down (transition: True → False).
            mock_dt.now.return_value = t2
            mock_docker.DockerClient.side_effect = DockerException("SSH refused")
            scanner_mod.run_check()

            # Scan 3 — remote still down (False → False, no transition).
            mock_dt.now.return_value = t3
            scanner_mod.run_check()

        row = {r["host"]: r for r in state_mod.get_host_status()}["remote-a"]
        # down_since is the transition time (scan 2), not the latest scan (3).
        assert row["down_since"] == t2.isoformat()
        # checked_at reflects the most recent attempt (scan 3).
        assert row["checked_at"] == t3.isoformat()
        assert row["reachable"] == 0
        assert row["error"] == "SSH refused"

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    @patch.object(scanner_mod, "datetime")
    def test_down_since_none_when_reachable(
        self, mock_dt, mock_docker, mock_token, mock_notify
    ):
        """A host that is reachable (or has recovered) has ``down_since=None``."""
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.return_value = _container_client("remote-a", [])

        t1 = _real_datetime(2026, 1, 1, 10, 0, 0, tzinfo=timezone.utc)
        t2 = _real_datetime(2026, 1, 1, 11, 0, 0, tzinfo=timezone.utc)

        with _patch_hosts(_TWO_HOSTS), \
             patch.object(config_mod, "GITHUB_TOKEN", ""):
            # Scan 1 — remote down (transition sets down_since).
            mock_dt.now.return_value = t1
            mock_docker.DockerClient.side_effect = DockerException("offline")
            scanner_mod.run_check()

            # Scan 2 — remote recovers (down_since must clear to None).
            mock_dt.now.return_value = t2
            mock_docker.DockerClient.side_effect = None
            mock_docker.DockerClient.return_value = _container_client("remote-a", [])
            scanner_mod.run_check()

        row = {r["host"]: r for r in state_mod.get_host_status()}["remote-a"]
        assert row["reachable"] == 1
        assert row["down_since"] is None
        assert row["error"] is None


# ---------------------------------------------------------------------------
# D3 — hosts removed from DOCKER_HOSTS have their stale rows purged
# ---------------------------------------------------------------------------

class TestRemovedHostPurged:
    """QA D3: a host dropped from DOCKER_HOSTS between two scans has its
    host_status row and pending updates removed, mirroring the metrics layer
    which already zeroes per-host series for hosts that fall out of the set.
    """

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.0", "1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_removed_host_status_and_pending_updates_purged(
        self, mock_docker, mock_token, mock_fetch, mock_notify
    ):
        three = [
            ("local", None),
            ("host-b", "ssh://u@b/tcp"),
            ("host-c", "ssh://u@c/tcp"),
        ]
        two = [h for h in three if h[0] != "host-c"]

        # host-c runs a container that has an available update so a pending
        # updates row exists for it; local + host-b run nothing.
        c_on_c = _monitored_container("web", "myapp:1.1")
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.side_effect = [
            _container_client("host-b", []),
            _container_client("host-c", [c_on_c]),
        ]

        # Scan 1 — all three configured; a pending updates row for host-c is
        # created.
        with _patch_hosts(three), patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        statuses1 = {r["host"] for r in state_mod.get_host_status()}
        assert "host-c" in statuses1
        pending_c = [
            r for r in state_mod.get_all_updates()
            if r["host"] == "host-c" and r["resolved_at"] is None
        ]
        assert pending_c, "expected a pending update row for host-c"

        # Scan 2 — host-c removed from DOCKER_HOSTS (shrunk host list). Its
        # rows must be purged; local + host-b untouched.
        mock_docker.from_env.return_value = _container_client("local", [])
        mock_docker.DockerClient.side_effect = [
            _container_client("host-b", []),
        ]
        with _patch_hosts(two), patch.object(config_mod, "GITHUB_TOKEN", ""):
            scanner_mod.run_check()

        # host_status no longer carries host-c…
        statuses2 = {r["host"] for r in state_mod.get_host_status()}
        assert "host-c" not in statuses2
        assert statuses2 == {"local", "host-b"}

        # …and no host-c update rows survive (pending purged; there were no
        # resolved rows for host-c here).
        assert not [r for r in state_mod.get_all_updates() if r["host"] == "host-c"]

        # In-memory transition tracking is pruned too, so a later re-add of
        # host-c starts from a clean baseline (no spurious down/recovered on
        # the first scan back).
        assert "host-c" not in scanner_mod._last_host_reachable
        assert "host-c" not in scanner_mod._last_host_errors

    @patch.object(scanner_mod, "notify")
    @patch.object(scanner_mod, "fetch_all_tags", return_value=["1.0", "1.1", "1.2"])
    @patch.object(scanner_mod, "get_dockerhub_token", return_value="tok")
    @patch.object(scanner_mod, "docker")
    def test_down_then_removed_then_readded_is_reprimed(
        self, mock_docker, mock_token, mock_fetch, mock_notify
    ):
        """Removal prunes the in-memory baseline, so re-adding a host starts
        fresh (primed) rather than firing a spurious down/recovered off a
        stale remembered state.

        Scenario: host-c is DOWN in scan 1 (records reachable=False + error in
        memory), removed in scan 2 (purged from DB and memory), re-added in
        scan 3 and now reachable. Without the memory prune, scan 3 would look
        like a False→True transition and fire a bogus "recovered" alert.
        """
        events = []
        mock_notify_host = MagicMock(side_effect=lambda evts: events.extend(evts))

        def _run(hosts, docker_side_effects):
            mock_docker.from_env.return_value = _container_client("local", [])
            mock_docker.DockerClient.side_effect = docker_side_effects
            with _patch_hosts(hosts), patch.object(config_mod, "GITHUB_TOKEN", ""), \
                 patch.object(scanner_mod, "notify_host_status", mock_notify_host):
                scanner_mod.run_check()

        # Scan 1: local + host-c, host-c DOWN.
        _run(
            [("local", None), ("host-c", "ssh://u@c/tcp")],
            [DockerException("SSH refused")],
        )
        assert scanner_mod._last_host_reachable.get("host-c") is False

        # Scan 2: host-c removed (only local remains) — purge runs.
        _run([("local", None)], [])
        # The removal must have pruned host-c's in-memory tracking…
        assert "host-c" not in scanner_mod._last_host_reachable
        assert "host-c" not in scanner_mod._last_host_errors
        # …and its DB rows.
        assert not any(r["host"] == "host-c" for r in state_mod.get_host_status())

        # Scan 3: host-c re-added and now reachable → primed, no spurious event.
        _run(
            [("local", None), ("host-c", "ssh://u@c/tcp")],
            [_container_client("host-c", [])],
        )
        status = {r["host"]: r for r in state_mod.get_host_status()}
        assert status["host-c"]["reachable"] == 1
        # No down/recovered event was ever fired for host-c (it was re-added,
        # not recovered).
        assert not [e for e in events if e.host == "host-c"]
        mock_notify_host.assert_not_called()
