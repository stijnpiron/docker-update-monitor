import base64
import json
from dataclasses import asdict

import requests

import app.config as _config
import app.http as _http
from app.models import UpdateInfo, RegexMismatch, ScanWarning


def _build_payload(
    updates: list[UpdateInfo],
    mismatches: list[RegexMismatch],
    warnings: list[ScanWarning],
) -> dict:
    """Group updates by status into a structured payload."""
    grouped: dict[str, list[dict]] = {"new": [], "known": [], "resolved": []}
    for u in updates:
        entry = asdict(u)
        del entry["status"]
        grouped.setdefault(u.status, []).append(entry)
    # Drop empty categories
    payload = {k: v for k, v in grouped.items() if v}
    if mismatches:
        payload["regex_mismatches"] = [asdict(m) for m in mismatches]
    if warnings:
        payload["warnings"] = [asdict(w) for w in warnings]
    return payload


def notify(
    updates: list[UpdateInfo],
    *,
    mismatches: list[RegexMismatch] | None = None,
    warnings: list[ScanWarning] | None = None,
) -> bool | None:
    """Send webhook notification.

    Returns True on successful delivery, False on a delivery attempt that
    failed (e.g. network error, non-2xx response), and None when no delivery
    was attempted (empty payload, DRY_RUN, missing endpoint).
    """
    if not updates and not mismatches and not warnings:
        return None

    payload = _build_payload(updates, mismatches or [], warnings or [])

    if _config.DRY_RUN:
        _config.log.info("DRY_RUN — would POST:\n" + json.dumps(payload, indent=2))
        return None

    if not _config.NOTIFY_ENDPOINT:
        _config.log.warning("No NOTIFY_ENDPOINT set; skipping notification.")
        _config.log.info("Updates found:\n" + json.dumps(payload, indent=2))
        return None

    headers = {"Content-Type": "application/json"}
    if _config.NOTIFY_AUTH_TYPE == "bearer" and _config.NOTIFY_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {_config.NOTIFY_AUTH_TOKEN}"
    elif _config.NOTIFY_AUTH_TYPE == "basic" and _config.NOTIFY_AUTH_TOKEN:
        encoded = base64.b64encode(_config.NOTIFY_AUTH_TOKEN.encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {encoded}"
    elif _config.NOTIFY_AUTH_TYPE and _config.NOTIFY_AUTH_TYPE not in ("bearer", "basic"):
        _config.log.warning(f"Unknown NOTIFY_AUTH_TYPE '{_config.NOTIFY_AUTH_TYPE}' — sending without authentication")

    try:
        resp = _http.http_session.post(
            _config.NOTIFY_ENDPOINT,
            json=payload,
            headers=headers,
            timeout=15,
        )
        resp.raise_for_status()
    except requests.RequestException as exc:
        _config.log.error(f"Failed to notify endpoint: {exc}")
        return False

    _config.log.info(f"Notified endpoint with {len(updates)} update(s)  →  HTTP {resp.status_code}")
    return True
