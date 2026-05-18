"""Prometheus metrics for Docker Update Monitor."""

from prometheus_client import Counter, Gauge

containers_monitored = Gauge(
    "dum_containers_monitored",
    "Number of containers with update-monitor labels",
)

updates_available = Gauge(
    "dum_updates_available",
    "Number of available updates by type",
    ["type"],
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

notifications_sent_total = Counter(
    "dum_notifications_sent_total",
    "Total notifications sent by channel",
    ["channel"],
)

# Tracks which update_type labels have been set, so we can zero them out when
# they are no longer present after a scan.
_seen_update_types: set[str] = set()


def update_after_scan(
    *,
    monitored: int,
    updates: list,
    duration_seconds: float,
    last_check_ts: float,
) -> None:
    """Update gauge metrics with the latest scan results.

    ``updates`` is a list of dicts (from get_all_updates()) or UpdateInfo objects.
    """
    global _seen_update_types

    containers_monitored.set(monitored)
    last_check_timestamp_seconds.set(last_check_ts)
    check_duration_seconds.set(duration_seconds)

    by_type: dict[str, int] = {}
    for u in updates:
        if isinstance(u, dict):
            status = u.get("status", "")
            utype = u.get("update_type", "unknown")
        else:
            status = u.status
            utype = u.update_type
        if status != "resolved":
            by_type[utype] = by_type.get(utype, 0) + 1

    for t in _seen_update_types - set(by_type):
        updates_available.labels(type=t).set(0)

    for t, count in by_type.items():
        updates_available.labels(type=t).set(count)

    _seen_update_types = set(by_type.keys())
