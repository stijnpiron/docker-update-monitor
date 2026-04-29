import json
from dataclasses import asdict

import app.config as _config
import app.http as _http
from app.models import UpdateInfo


def _build_payload(updates: list[UpdateInfo]) -> dict:
    """Group updates by status into a structured payload."""
    grouped: dict[str, list[dict]] = {"new": [], "known": [], "resolved": []}
    for u in updates:
        entry = asdict(u)
        del entry["status"]
        grouped.setdefault(u.status, []).append(entry)
    # Drop empty categories
    return {k: v for k, v in grouped.items() if v}


def notify(updates: list[UpdateInfo]) -> None:
    if not updates:
        return

    payload = _build_payload(updates)

    if _config.DRY_RUN:
        _config.log.info("DRY_RUN — would POST:\n" + json.dumps(payload, indent=2))
        return

    if not _config.NOTIFY_ENDPOINT:
        _config.log.warning("No NOTIFY_ENDPOINT set; skipping notification.")
        _config.log.info("Updates found:\n" + json.dumps(payload, indent=2))
        return

    headers = {"Content-Type": "application/json"}
    if _config.NOTIFY_AUTH_TYPE == "bearer" and _config.NOTIFY_AUTH_TOKEN:
        headers["Authorization"] = f"Bearer {_config.NOTIFY_AUTH_TOKEN}"
    elif _config.NOTIFY_AUTH_TYPE == "basic" and _config.NOTIFY_AUTH_TOKEN:
        headers["Authorization"] = f"Basic {_config.NOTIFY_AUTH_TOKEN}"
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
        _config.log.info(f"Notified endpoint with {len(updates)} update(s)  →  HTTP {resp.status_code}")
    except Exception as exc:
        _config.log.error(f"Failed to notify endpoint: {exc}")
