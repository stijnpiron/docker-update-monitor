"""Unit tests for the scan data model (host field + HostStatus) — task 02."""

from dataclasses import asdict

from app.models import UpdateInfo, RegexMismatch, ScanWarning, HostStatus


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


def _make_mismatch(**overrides) -> RegexMismatch:
    base = dict(
        container_name="app",
        service_name="app",
        stack="stack",
        image="nginx",
        current_tag="weird_tag",
        pattern=r"^(\d+)\.(\d+)$",
        reason="did not match current tag",
    )
    base.update(overrides)
    return RegexMismatch(**base)


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


# --- RegexMismatch / ScanWarning ------------------------------------------------

def test_regex_mismatch_and_scan_warning_default_local():
    assert _make_mismatch().host == "local"
    assert _make_warning().host == "local"


def test_regex_mismatch_accepts_explicit_host():
    assert _make_mismatch(host="prod-1").host == "prod-1"


def test_scan_warning_accepts_explicit_host():
    assert _make_warning(host="prod-1").host == "prod-1"


# --- HostStatus ----------------------------------------------------------------

def test_host_status_fields_round_trip():
    """AC3: fields round-trip; error is None when reachable is True
    (a documented convention, not enforced by the type)."""
    hs = HostStatus(
        host="prod-1",
        reachable=True,
        error=None,
        checked_at="2026-09-04T12:00:00+00:00",
    )
    assert hs.host == "prod-1"
    assert hs.reachable is True
    assert hs.error is None
    assert hs.checked_at == "2026-09-04T12:00:00+00:00"

    # Unreachable case carries an error message by convention.
    hs_down = HostStatus(
        host="prod-2",
        reachable=False,
        error="ssh: Connection timed out",
        checked_at="2026-09-04T12:05:00+00:00",
    )
    assert hs_down.reachable is False
    assert hs_down.error == "ssh: Connection timed out"


# --- Webhook payload serialization ----------------------------------------------

def test_webhook_asdict_includes_host():
    """AC5: _build_payload serializes host on every update / mismatch / warning."""
    from app.notifications.webhook import _build_payload

    updates = [
        _make_update(host="prod-1", status="new"),
        _make_update(image="redis", host="prod-2", status="known"),
    ]
    mismatches = [_make_mismatch(host="prod-1")]
    warnings = [_make_warning(host="prod-2")]

    payload = _build_payload(updates, mismatches, warnings)

    # Every grouped update entry carries host.
    for status_key, entries in payload.items():
        if status_key in ("new", "known", "resolved"):
            assert all("host" in e for e in entries)
    assert payload["new"][0]["host"] == "prod-1"
    assert payload["known"][0]["host"] == "prod-2"

    # Mismatch / warning lists also carry host.
    assert payload["regex_mismatches"][0]["host"] == "prod-1"
    assert payload["warnings"][0]["host"] == "prod-2"
