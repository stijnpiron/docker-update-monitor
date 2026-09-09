"""Prometheus metrics for Docker Update Monitor."""

from prometheus_client import Counter, Gauge

containers_monitored = Gauge(
    "dum_containers_monitored",
    "Number of containers with update-monitor labels",
)

updates_available = Gauge(
    "dum_updates_available",
    "Number of available updates by type and host",
    ["type", "host"],
)

host_reachable = Gauge(
    "dum_host_reachable",
    "Per-host reachability gauge (1=reachable, 0=unreachable)",
    ["host"],
)

check_duration_seconds = Gauge(
    "dum_check_duration_seconds",
    "Duration of the last update check",
)

check_errors_total = Counter(
    "dum_check_errors_total",
    "Total number of errors during checks",
)

last_check_timestamp_seconds = Gauge(
    "dum_last_check_timestamp_seconds",
    "Unix timestamp of last completed check",
)

notifications_attempted_total = Counter(
    "dum_notifications_attempted_total",
    "Total notifications attempted (delivery tried) by channel",
    ["channel"],
)

notifications_sent_total = Counter(
    "dum_notifications_sent_total",
    "Total notifications successfully delivered by channel",
    ["channel"],
)

# Tracks which (host, update_type) label pairs have been set, so we can zero
# them out when they are no longer present after a scan.
_seen_update_labels: set[tuple[str, str]] = set()

# Tracks which host labels have been set on dum_host_reachable, so we can zero
# them out when a host is no longer configured/present after a scan.
_seen_hosts: set[str] = set()


def _iter_update_fields(updates: list) -> list[tuple[str, str, str]]:
    """Return (status, update_type, host) for each update dict or object.

    A missing/empty ``host`` falls back to ``"local"`` so single-host (pre-
    feature) data keeps its legacy labels untouched.
    """
    out: list[tuple[str, str, str]] = []
    for u in updates:
        if isinstance(u, dict):
            out.append((u.get("status", ""), u.get("update_type", "unknown"), u.get("host") or "local"))
        else:
            out.append((u.status, u.update_type, getattr(u, "host", "local") or "local"))
    return out


def update_after_scan(
    *,
    monitored: int,
    updates: list,
    duration_seconds: float,
    last_check_ts: float,
    host_status: list[dict] | None = None,
) -> None:
    """Update gauge metrics with the latest scan results.

    ``updates`` is a list of dicts (from get_all_updates()) or UpdateInfo
    objects; each carries a ``host`` (defaults to ``"local"``).

    ``host_status`` is an optional list of ``{"host": ..., "reachable": ...}``
    rows (one per configured host) driving ``dum_host_reachable``. Pass ``None``
    to leave the per-host reachability gauge untouched.
    """
    global _seen_update_labels, _seen_hosts

    containers_monitored.set(monitored)
    last_check_timestamp_seconds.set(last_check_ts)
    check_duration_seconds.set(duration_seconds)

    # Per-host, per-type pending counts (status != resolved).
    by_host_type: dict[tuple[str, str], int] = {}
    for status, utype, host in _iter_update_fields(updates):
        if status != "resolved":
            by_host_type[(host, utype)] = by_host_type.get((host, utype), 0) + 1

    for host, t in _seen_update_labels - set(by_host_type):
        updates_available.labels(type=t, host=host).set(0)

    for (host, t), count in by_host_type.items():
        updates_available.labels(type=t, host=host).set(count)

    _seen_update_labels = set(by_host_type)

    # Per-host reachability gauge (dum_host_reachable).
    if host_status is not None:
        current_hosts = {row["host"] for row in host_status}
        for host in _seen_hosts - current_hosts:
            host_reachable.labels(host=host).set(0)
        for row in host_status:
            host = row["host"]
            reachable = row.get("reachable")
            up = 1 if reachable else 0
            host_reachable.labels(host=host).set(up)
        _seen_hosts = set(current_hosts)
