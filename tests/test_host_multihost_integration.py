"""Full-stack multi-host integration test (task 09).

Drives a real ``app.scanner.run_check()`` against two or more mocked Docker
clients (local + remote(s)) while the DB, dashboard, and notification layers
run unmocked, so the test crosses every seam of the feature at once:

- config → scanner loop
- scanner → per-host ``process_scan`` (DB)
- scanner → ``notify`` (update dispatch) and ``notify_host_status`` (down/recovered)
- dashboard Flask app → ``/api/updates`` and ``/api/host-status``

This is the test that stands in for the real-SSH smoke that task 08 could not
run (no live remote daemon in the environment).
"""

from unittest.mock import MagicMock, patch

import pytest

from docker.errors import DockerException

from app import config as config_mod
from app import scanner as scanner_mod
from app import state as state_mod
from app.dashboard import create_app


# The host list under test: the local daemon plus two remote hosts.
_HOSTS = [
    ("local", None),
    ("host-b", "ssh://user@hostb/tcp"),
    ("host-c", "ssh://user@hostc/tcp"),
]
_LOCAL_ONLY = [("local", None)]

_SEMVER_PATTERN = r"^(\d+)\.(\d+)$"
# Available tags: 1.2 is a minor update over the running 1.1.
_TAGS = ["1.0", "1.1", "1.2"]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _monitored_container(name, image_tag="myapp:1.1"):
    c = MagicMock(name=f"container-{name}")
    c.name = name
    c.labels = {f"{config_mod.LABEL_PREFIX}.tag-regex": _SEMVER_PATTERN}
    c.image.tags = [image_tag]
    c.image.attrs = {}
    c.attrs = {"Config": {"Image": image_tag}}
    return c


def _client(containers, all_containers=None):
    """A mock Docker client. ``containers.list()`` → running; ``(all=True)`` →
    *all_containers* (or the running list when omitted)."""
    c = MagicMock()
    ac = list(all_containers) if all_containers is not None else list(containers)

    def _list(all=False):
        return ac if all else list(containers)

    c.containers.list.side_effect = _list
    return c


def _scan(local_client, remote_clients, hosts, notify, host_status):
    """Run one ``run_check()`` with the given per-host clients.

    ``remote_clients`` is a list of client mocks (or exception instances) in
    host order for every non-local host; they feed ``docker.DockerClient``
    ``side_effect`` in call order.
    """
    with (
        patch.object(config_mod, "DOCKER_HOSTS", hosts),
        patch.object(config_mod, "GITHUB_TOKEN", ""),
        patch.object(scanner_mod, "get_dockerhub_token", return_value="tok"),
        patch.object(scanner_mod, "fetch_all_tags", return_value=list(_TAGS)),
        patch.object(scanner_mod, "docker") as mock_docker,
        patch.object(scanner_mod, "notify", notify),
        patch.object(scanner_mod, "notify_host_status", host_status),
    ):
        mock_docker.from_env.return_value = local_client
        if len(remote_clients) == 1:
            mock_docker.DockerClient.side_effect = remote_clients[0]
        else:
            mock_docker.DockerClient.side_effect = remote_clients
        scanner_mod.run_check()


class _Dashboard:
    """Thin wrapper around a live Flask test client for the two APIs."""

    def __init__(self):
        self.app = create_app()
        self.client = self.app.test_client()

    def updates(self):
        resp = self.client.get("/api/updates")
        assert resp.status_code == 200
        return resp.get_json()

    def host_status(self):
        resp = self.client.get("/api/host-status")
        assert resp.status_code == 200
        return resp.get_json()


@pytest.fixture(autouse=True)
def _reset_scanner():
    """Clear module-level reachability tracking so each test starts cold."""
    yield
    scanner_mod._last_host_reachable.clear()
    scanner_mod._last_host_errors.clear()


@pytest.fixture
def _collect_host_events():
    """A MagicMock that records every HostStatusEvent it is called with."""
    m = MagicMock(name="notify_host_status")
    events = []

    def _record(evts):
        events.extend(evts)

    m.side_effect = _record
    m.collected = events
    return m


# ---------------------------------------------------------------------------
# Test 1 — full run across three hosts (2 reachable + 1 unreachable)
# ---------------------------------------------------------------------------

def test_multihost_full_run_check_integration(_collect_host_events):
    notify1 = MagicMock(name="notify-scan1")
    notify2 = MagicMock(name="notify-scan2")

    # --- Scan 1: all three hosts reachable. Establishes the baseline
    # (``_last_host_reachable``) so the later down event can be detected.
    # ``shared`` runs on local + host-b; ``web`` only on local.
    shared_local = _monitored_container("shared")
    shared_b = _monitored_container("shared")
    web_local = _monitored_container("web", "webapp:1.1")

    _scan(
        _client([shared_local, web_local]),
        [_client([shared_b]), _client([])],
        _HOSTS, notify1, _collect_host_events,
    )

    # --- Scan 2: host-c is now unreachable, and ``shared`` is removed from the
    # local daemon while it still runs on host-b (the critical cross-host
    # isolation guard).
    _scan(
        _client([]),  # local: shared removed, web removed too
        [_client([_monitored_container("shared")]), DockerException("SSH refused")],
        _HOSTS, notify2, _collect_host_events,
    )

    db = state_mod.get_all_updates()

    # host-tagged rows: host-b still has the surviving ``shared`` update.
    b_shared = [r for r in state_mod.get_active_updates()
                if r["host"] == "host-b" and r["container_name"] == "shared"]
    assert b_shared, "host-b ``shared`` row must survive"

    # The local ``shared`` row was cleaned up (container removed on local).
    assert not [r for r in state_mod.get_active_updates()
                if r["host"] == "local" and r["container_name"] == "shared"], \
        "local ``shared`` row must be cleaned up on removal"

    # host-c never produced update rows…
    assert not [r for r in db if r["host"] == "host-c"], \
        "unreachable host must have no update rows"

    # …but it has a host_status row (reachable=0, non-null error).
    status = {s["host"]: s for s in state_mod.get_host_status()}
    assert set(status) == {"local", "host-b", "host-c"}
    assert status["local"]["reachable"] == 1
    assert status["host-b"]["reachable"] == 1
    assert status["host-c"]["reachable"] == 0
    assert status["host-c"]["error"]

    # No host-event alert fires on the first scan (first-scan priming: no
    # baseline yet); the down alert only fires once host-c transitions.
    downs = [e for e in _collect_host_events.collected
             if e.event == "down" and e.host == "host-c"]
    assert len(downs) == 1

    # The second scan dispatched exactly one update notification, and every
    # actionable row carries the correct host.
    notify2.assert_called_once()
    rows = notify2.call_args.args[0]
    assert any(u.host == "host-b" for u in rows)
    assert all(u.host in {"local", "host-b"} for u in rows)

    # Dashboard agrees: /api/host-status shows 2 up + 1 down; /api/updates
    # contains the surviving hosts (never the unreachable one).
    dash = _Dashboard()
    api_status = {s["host"]: s for s in dash.host_status()}
    ups = [h for h, s in api_status.items() if s["reachable"] == 1]
    downs_api = [h for h, s in api_status.items() if s["reachable"] == 0]
    assert set(ups) == {"local", "host-b"}
    assert downs_api == ["host-c"]

    api_update_hosts = {u["host"] for u in dash.updates() if not u.get("resolved_at")}
    assert "host-c" not in api_update_hosts
    assert "host-b" in api_update_hosts


# ---------------------------------------------------------------------------
# Test 2 — single host is backward compatible (all ``local``)
# ---------------------------------------------------------------------------

def test_single_host_integration_is_backward_compatible(_collect_host_events):
    notify = MagicMock(name="notify")
    _scan(
        _client([_monitored_container("web", "webapp:1.1")]),
        [],
        _LOCAL_ONLY, notify, _collect_host_events,
    )

    rows = state_mod.get_all_updates()
    assert rows, "expected at least one local update row"
    assert all(r["host"] == "local" for r in rows)

    status = state_mod.get_host_status()
    assert [s["host"] for s in status] == ["local"]
    assert status[0]["reachable"] == 1

    notify.assert_called_once()
    assert all(u.host == "local" for u in notify.call_args.args[0])


# ---------------------------------------------------------------------------
# Test 3 — reachability transition drives exactly one down + one recovered
# ---------------------------------------------------------------------------

def test_reachability_transition_triggers_recovery_alert(_collect_host_events):
    hosts = [("local", None), ("remote", "ssh://user@remote/tcp")]
    empty = _client([])

    # Scan A: remote up — first-scan priming (no alert).
    _scan(empty, [_client([])], hosts, MagicMock(), _collect_host_events)
    # Scan B: remote down — down alert.
    _scan(empty, [DockerException("SSH refused")], hosts, MagicMock(), _collect_host_events)
    # Scan C: remote back up — recovered alert.
    _scan(empty, [_client([])], hosts, MagicMock(), _collect_host_events)

    events = _collect_host_events.collected
    downs = [e for e in events if e.event == "down"]
    recovered = [e for e in events if e.event == "recovered"]
    assert len(downs) == 1, f"expected one down alert, got {[ (e.host, e.event) for e in events ]}"
    assert len(recovered) == 1, "expected exactly one recovered alert"
    assert downs[0].host == "remote"
    assert recovered[0].host == "remote"
