import threading
import time
from datetime import datetime, timezone

from app.state import save_last_check

_start_time = time.monotonic()

_state_lock = threading.Lock()
_state: dict = {
    "last_check": None,
    "next_check": None,
    "containers_monitored": 0,
    "warnings": [],
    "skipped_containers": [],
}


def update_state(*, last_check: datetime | None = None, next_check: datetime | None = None,
                 containers_monitored: int | None = None,
                 warnings: list[dict] | None = None,
                 skipped_containers: list[dict] | None = None) -> None:
    iso_to_persist: str | None = None
    with _state_lock:
        if last_check is not None:
            iso = last_check.isoformat().replace("+00:00", "Z")
            _state["last_check"] = iso
            iso_to_persist = iso
        if next_check is not None:
            _state["next_check"] = next_check.isoformat().replace("+00:00", "Z")
        if containers_monitored is not None:
            _state["containers_monitored"] = containers_monitored
        if warnings is not None:
            _state["warnings"] = list(warnings)
        if skipped_containers is not None:
            _state["skipped_containers"] = list(skipped_containers)
    if iso_to_persist is not None:
        save_last_check(iso_to_persist)


def _build_response() -> tuple[int, dict]:
    with _state_lock:
        last_check = _state["last_check"]
        next_check = _state["next_check"]
        containers_monitored = _state["containers_monitored"]
        # warnings and skipped_containers are intentionally excluded from the
        # health endpoint to keep it lightweight. The dashboard serves the full state.

    body = {
        "status": "ok",
        "last_check": last_check,
        "next_check": next_check,
        "containers_monitored": containers_monitored,
        "uptime_seconds": int(time.monotonic() - _start_time),
    }

    if last_check is None:
        body["status"] = "starting"
        body["note"] = "waiting for first scan to complete"

    return 200, body
