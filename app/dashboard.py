"""Flask web dashboard for Docker Update Monitor."""

import threading

from flask import Flask, jsonify, render_template

import app.config as _config
from app.health import update_state, _state, _state_lock, _build_response
from app.state import get_all_updates

_scan_trigger = threading.Event()


def create_app() -> Flask:
    """Create and configure the Flask application."""
    application = Flask(__name__, template_folder="templates")

    @application.route("/")
    def dashboard():
        updates = get_all_updates()
        with _state_lock:
            last_check = _state.get("last_check")
            next_check = _state.get("next_check")
            containers_monitored = _state.get("containers_monitored", 0)

        new_count = sum(1 for u in updates if u["status"] == "new")
        known_count = sum(1 for u in updates if u["status"] == "known")
        resolved_count = sum(1 for u in updates if u["status"] == "resolved")

        return render_template(
            "dashboard.html",
            updates=updates,
            last_check=last_check,
            next_check=next_check,
            containers_monitored=containers_monitored,
            new_count=new_count,
            known_count=known_count,
            resolved_count=resolved_count,
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
    """Start the Flask dashboard in a daemon thread."""
    if port is None:
        port = _config.WEB_PORT

    application = create_app()
    thread = threading.Thread(
        target=lambda: application.run(host=host, port=port, use_reloader=False),
        daemon=True,
    )
    thread.start()
    _config.log.info(f"Dashboard listening on http://{host}:{port}")
    return thread
