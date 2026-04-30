import json
import threading
import time
from datetime import datetime, timezone
from http.server import HTTPServer, BaseHTTPRequestHandler

import app.config as _config

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
    with _state_lock:
        if last_check is not None:
            _state["last_check"] = last_check.isoformat().replace("+00:00", "Z")
        if next_check is not None:
            _state["next_check"] = next_check.isoformat().replace("+00:00", "Z")
        if containers_monitored is not None:
            _state["containers_monitored"] = containers_monitored
        if warnings is not None:
            _state["warnings"] = warnings
        if skipped_containers is not None:
            _state["skipped_containers"] = skipped_containers


def _build_response() -> tuple[int, dict]:
    with _state_lock:
        last_check = _state["last_check"]
        next_check = _state["next_check"]
        containers_monitored = _state["containers_monitored"]

    if last_check is None:
        return 503, {"status": "unavailable", "reason": "no check completed yet"}

    return 200, {
        "status": "ok",
        "last_check": last_check,
        "next_check": next_check,
        "containers_monitored": containers_monitored,
        "uptime_seconds": int(time.monotonic() - _start_time),
    }


class _HealthHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            status_code, body = _build_response()
            payload = json.dumps(body).encode()
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, format, *args):
        pass  # suppress access logs


def start_health_server() -> threading.Thread:
    server = HTTPServer(("0.0.0.0", _config.WEB_PORT), _HealthHandler)
    server.allow_reuse_address = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    _config.log.info(f"Health endpoint listening on port {_config.WEB_PORT}")
    return thread
