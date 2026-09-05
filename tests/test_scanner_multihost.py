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
from unittest.mock import MagicMock, patch

import pytest

from docker.errors import DockerException

from app import config as config_mod
from app import health as health_mod
from app import scanner as scanner_mod
from app import state as state_mod


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
    def test_local_unreachable_early_return(
        self, mock_docker, mock_token, mock_notify, caplog
    ):
        """A single unreachable local host ends the check without dispatch
        (preserves the pre-feature log message)."""
        mock_docker.from_env.side_effect = DockerException("cannot connect")

        with _patch_hosts(_LOCAL_ONLY), \
             patch.object(config_mod, "GITHUB_TOKEN", ""), \
             caplog.at_level(logging.ERROR):
            scanner_mod.run_check()

        assert "Cannot connect to Docker" in caplog.text
        mock_notify.assert_not_called()


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
