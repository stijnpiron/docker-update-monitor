"""Flask web dashboard for Docker Update Monitor."""

import threading
from datetime import datetime

from flask import Flask, jsonify, render_template

import app.config as _config
from app.health import update_state, _state, _state_lock, _build_response
from app.state import get_all_updates

_scan_trigger = threading.Event()


def _format_datetime(iso_str: str | None) -> str:
    """Format an ISO datetime string using the configured display format."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        return dt.strftime(_config.DASHBOARD_DATETIME_FORMAT)
    except (ValueError, TypeError):
        return iso_str


def create_app() -> Flask:
    """Create and configure the Flask application."""
    application = Flask(__name__, template_folder="templates")

    @application.route("/")
    def dashboard():
        updates = get_all_updates()

        # Format first_seen_at for display
        for u in updates:
            u["first_seen_at_display"] = _format_datetime(u.get("first_seen_at"))

        # Sort by stack (default)
        updates.sort(key=lambda u: (u.get("stack") or "", u.get("container_name") or ""))

        with _state_lock:
            last_check = _state.get("last_check")
            next_check = _state.get("next_check")
            containers_monitored = _state.get("containers_monitored", 0)
            warnings = list(_state.get("warnings", []))
            skipped_containers = list(_state.get("skipped_containers", []))

        # Format scan times for display
        last_check_display = _format_datetime(last_check) if last_check else "never"
        next_check_display = _format_datetime(next_check) if next_check else "—"

        # Sort skipped by stack then name
        skipped_containers.sort(key=lambda c: (c.get("stack") or "", c.get("container_name") or ""))

        new_count = sum(1 for u in updates if u["status"] == "new")
        known_count = sum(1 for u in updates if u["status"] == "known")
        resolved_count = sum(1 for u in updates if u["status"] == "resolved")

        return render_template(
            "dashboard.html",
            updates=updates,
            last_check=last_check_display,
            next_check=next_check_display,
            containers_monitored=containers_monitored,
            new_count=new_count,
            known_count=known_count,
            resolved_count=resolved_count,
            warnings=warnings,
            skipped_containers=skipped_containers,
        )

    @application.route("/health")
    def health():
        status_code, body = _build_response()
        return jsonify(body), status_code

    @application.route("/api/updates")
    def api_updates():
        updates = get_all_updates()
        return jsonify(updates)

    @application.route("/api/scan", methods=["POST"])
    def api_scan():
        _scan_trigger.set()
        return jsonify({"message": "Scan triggered"}), 202

    return application


def start_dashboard(host: str = "0.0.0.0", port: int | None = None) -> threading.Thread:
    """Start the Flask dashboard in a daemon thread using waitress."""
    from waitress import serve

    if port is None:
        port = _config.WEB_PORT

    application = create_app()
    thread = threading.Thread(
        target=lambda: serve(application, host=host, port=port, _quiet=True),
        daemon=True,
    )
    thread.start()
    _config.log.info(f"Dashboard listening on http://{host}:{port}")
    return thread
