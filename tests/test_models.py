"""Unit tests for the scan data model (host field) — task 02."""

from dataclasses import asdict

from app.models import UpdateInfo, ScanWarning


# --- Construction helpers shared across tests ---------------------------------

def _make_update(**overrides) -> UpdateInfo:
    base = dict(
        container_name="app",
        service_name="app",
        stack="stack",
        image="nginx",
        current_version="1.0.0",
        new_version="2.0.0",
        update_type="major",
    )
    base.update(overrides)
    return UpdateInfo(**base)


def _make_warning(**overrides) -> ScanWarning:
    base = dict(
        container_name="app",
        image="nginx",
        level="warning",
        message="something went off",
    )
    base.update(overrides)
    return ScanWarning(**base)


# --- UpdateInfo ----------------------------------------------------------------

def test_update_info_defaults_to_local():
    """AC1: omitting `host` yields 'local' (backward compatible)."""
    assert _make_update().host == "local"


def test_update_info_accepts_explicit_host():
    u = _make_update(host="prod-1")
    assert u.host == "prod-1"


def test_update_info_positional_host_placement():
    """AC2: positional construction through `update_type` (7th) still works,
    and an 8th positional maps to `host` per the required field order."""
    # 7 positional — every existing call site uses at most this.
    seven = UpdateInfo(
        "c1", "s", "st", "img", "1.0", "2.0", "major",
    )
    assert seven.host == "local"
    assert seven.status == ""

    # 8th positional lands on `host`, not `status`.
    eight = UpdateInfo(
        "c1", "s", "st", "img", "1.0", "2.0", "major", "prod-1",
    )
    assert eight.host == "prod-1"
    assert eight.status == ""


def test_update_info_asdict_includes_host():
    """AC4: asdict carries the host key."""
    u = _make_update(host="prod-1")
    d = asdict(u)
    assert "host" in d
    assert d["host"] == "prod-1"


# --- ScanWarning ----------------------------------------------------------------

def test_scan_warning_default_local():
    assert _make_warning().host == "local"


def test_scan_warning_accepts_explicit_host():
    assert _make_warning(host="prod-1").host == "prod-1"


# --- HostStatusEvent ----------------------------------------------------------------

def test_host_status_event_summary():
    from app.models import HostStatusEvent
    assert HostStatusEvent(host="h1", event="recovered").summary == "Host recovered: h1"
    assert HostStatusEvent(host="h1", event="down", error="boom").summary == "Host down: h1 — boom"
    assert HostStatusEvent(host="h1", event="down").summary == "Host down: h1 — unreachable"


# --- Webhook payload serialization ----------------------------------------------

def test_webhook_asdict_includes_host():
    """AC5: _build_payload serializes host on every update / warning."""
    from app.notifications.webhook import _build_payload

    updates = [
        _make_update(host="prod-1", status="new"),
        _make_update(image="redis", host="prod-2", status="known"),
    ]
    warnings = [_make_warning(host="prod-2")]

    payload = _build_payload(updates, warnings)

    # Every grouped update entry carries host.
    for status_key, entries in payload.items():
        if status_key in ("new", "known", "resolved"):
            assert all("host" in e for e in entries)
    assert payload["new"][0]["host"] == "prod-1"
    assert payload["known"][0]["host"] == "prod-2"

    # Warning list also carries host.
    assert payload["warnings"][0]["host"] == "prod-2"
