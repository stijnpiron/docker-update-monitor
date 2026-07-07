import app.config as _config
from app.models import UpdateInfo, RegexMismatch, ScanWarning
from app.notifications.webhook import notify as webhook_notify
from app.notifications.email import notify as email_notify
from app.metrics import notifications_attempted_total, notifications_sent_total


def _record(channel: str, result: bool | None) -> None:
    """Update attempted/sent counters based on a notifier return value."""
    if result is None:
        # Notifier skipped (no payload, dry-run, missing config) — no attempt made.
        return
    notifications_attempted_total.labels(channel=channel).inc()
    if result:
        notifications_sent_total.labels(channel=channel).inc()


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
            result = webhook_notify(updates, mismatches=mismatches or [], warnings=warnings or [])
            _record("webhook", result)
        elif channel == "email":
            result = email_notify(updates, mismatches=mismatches or [], warnings=warnings or [])
            _record("email", result)
        else:
            _config.log.warning(f"Unknown notification channel '{channel}' — skipping")
