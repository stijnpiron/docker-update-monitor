"""Flask web dashboard for Docker Update Monitor."""

import hashlib
import threading
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Flask, Response, jsonify, render_template
from prometheus_client import generate_latest, CONTENT_TYPE_LATEST

import app.config as _config
from app.health import _state, _state_lock, _build_response
from app.state import get_all_updates, get_host_status

_scan_trigger = threading.Event()


def _format_datetime(iso_str: str | None) -> str:
    """Format an ISO datetime string using the configured display format."""
    if not iso_str:
        return "—"
    try:
        dt = datetime.fromisoformat(iso_str)
        if dt.tzinfo is not None:
            tz_name = _config.TZ
            if tz_name:
                try:
                    dt = dt.astimezone(ZoneInfo(tz_name))
                except ZoneInfoNotFoundError:
                    _config.log.warning("Unknown timezone %r in TZ env var, falling back to system local timezone", tz_name)
                    dt = dt.astimezone()
            else:
                dt = dt.astimezone()
        return dt.strftime(_config.DASHBOARD_DATETIME_FORMAT)
    except (ValueError, TypeError):
        return iso_str


def _host_badge_class(host: str) -> str:
    """Return a CSS class (``host-badge-{N}``) derived deterministically from *host*.

    The same host name always maps to the same class, so the badge color is
    stable across reloads and pages (see task 06, AC1).
    """
    digest = hashlib.md5((host or "local").encode("utf-8")).digest()
    return f"host-badge-{digest[0] % 12}"


def _host_sort_key(host: str | None) -> str:
    """Missing/empty host sorts as ``local`` (the pre-feature default)."""
    return host or "local"


def create_app() -> Flask:
    """Create and configure the Flask application."""
    application = Flask(__name__, template_folder="templates")

    @application.route("/")
    def dashboard():
        updates = get_all_updates()

        # Format first_seen_at for display
        for u in updates:
            u["first_seen_at_display"] = _format_datetime(u.get("first_seen_at"))

        # Default sort: host, stack, container name (multi-host support, task 06).
        updates.sort(key=lambda u: (_host_sort_key(u.get("host")), u.get("stack") or "", u.get("container_name") or ""))

        with _state_lock:
            last_check = _state.get("last_check")
            next_check = _state.get("next_check")
            containers_monitored = _state.get("containers_monitored", 0)
            warnings = list(_state.get("warnings", []))
            skipped_containers = list(_state.get("skipped_containers", []))

        # Status strip: read reachability straight from the DB (task 06 spec),
        # and seed every configured host as "unknown" so a host with zero
        # pending updates still appears (AC4) even before the first scan.
        host_status_rows = [dict(r) for r in get_host_status()]
        seen = {r["host"] for r in host_status_rows}
        for name, _url in _config.DOCKER_HOSTS:
            if name not in seen:
                host_status_rows.append({"host": name, "reachable": None, "error": None, "checked_at": None})
        host_status_rows.sort(key=lambda r: r["host"])
        for row in host_status_rows:
            raw = row.get("reachable")
            row["reachable"] = bool(raw) if isinstance(raw, int) else None
            row["checked_at_display"] = _format_datetime(row.get("checked_at"))
            # The "unreachable since" label shows the *transition* time (start of
            # the current outage), not the latest check (D2). Fall back to
            # checked_at when down_since is absent (e.g. pre-migration rows).
            row["down_since_display"] = _format_datetime(
                row.get("down_since") or row.get("checked_at")
            )
            row["badge_class"] = _host_badge_class(row.get("host") or "local")

        # Attach the deterministic badge class to each update + skipped row
        # once in Python so the template does not need access to the helper.
        for u in updates:
            u["host"] = u.get("host") or "local"
            u["host_badge_class"] = _host_badge_class(u["host"])
        for c in skipped_containers:
            c["host"] = c.get("host") or "local"
            c["host_badge_class"] = _host_badge_class(c["host"])
        for w in warnings:
            w["host"] = w.get("host") or "local"
            w["host_badge_class"] = _host_badge_class(w["host"])

        # Sort skipped + warnings by host, stack, name (task 06)
        skipped_containers.sort(key=lambda c: (c["host"], c.get("stack") or "", c.get("container_name") or ""))
        warnings.sort(key=lambda w: (w["host"], w.get("stack") or "", w.get("container_name") or ""))

        # Format scan times for display
        last_check_display = _format_datetime(last_check) if last_check else "never"
        next_check_display = _format_datetime(next_check) if next_check else "—"

        new_count = sum(1 for u in updates if u["status"] == "new")
        known_count = sum(1 for u in updates if u["status"] == "known")
        resolved_count = sum(1 for u in updates if u["status"] == "resolved")

        return render_template(
            "dashboard.html",
            updates=updates,
            host_status=host_status_rows,
            last_check=last_check_display,
            last_check_raw=last_check or "",
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
        # Sort mirrors the page default (host, stack, container_name) per task 06.
        updates = get_all_updates()
        updates.sort(key=lambda u: (_host_sort_key(u.get("host")), u.get("stack") or "", u.get("container_name") or ""))
        return jsonify(updates)

    @application.route("/api/host-status")
    def api_host_status():
        """Return per-host reachability rows so the strip is consumable programmatically."""
        return jsonify(get_host_status())

    @application.route("/api/scan", methods=["POST"])
    def api_scan():
        _scan_trigger.set()
        return jsonify({"message": "Scan triggered"}), 202

    @application.route("/api/last-scan")
    def api_last_scan():
        with _state_lock:
            last_check = _state.get("last_check")
        return jsonify({"last_check": last_check})

    @application.route("/metrics")
    def metrics():
        return Response(generate_latest(), mimetype=CONTENT_TYPE_LATEST)

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
