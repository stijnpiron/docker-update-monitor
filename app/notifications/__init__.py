import app.config as _config
from app.models import UpdateInfo, RegexMismatch, ScanWarning
from app.notifications.webhook import notify as webhook_notify
from app.notifications.email import notify as email_notify


def dispatch(
    updates: list[UpdateInfo],
    *,
    mismatches: list[RegexMismatch] | None = None,
    warnings: list[ScanWarning] | None = None,
) -> None:
    """Send notifications via all configured channels."""
    if not updates and not mismatches and not warnings:
        return

    for channel in _config.NOTIFY_CHANNELS:
        if channel == "webhook":
            webhook_notify(updates, mismatches=mismatches or [], warnings=warnings or [])
        elif channel == "email":
            email_notify(updates, mismatches=mismatches or [], warnings=warnings or [])
        else:
            _config.log.warning(f"Unknown notification channel '{channel}' — skipping")
