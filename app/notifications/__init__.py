import app.config as _config
from app.models import UpdateInfo, ScanWarning, HostStatusEvent
from app.notifications.webhook import notify as webhook_notify
from app.notifications.email import notify as email_notify
from app.notifications.host_status import notify_host_status
from app.metrics import record_delivery

__all__ = ["dispatch", "notify_host_status", "HostStatusEvent"]


def dispatch(
    updates: list[UpdateInfo],
    *,
    warnings: list[ScanWarning] | None = None,
) -> None:
    """Send notifications via all configured channels."""
    if not updates and not warnings:
        return

    for channel in _config.NOTIFY_CHANNELS:
        if channel == "webhook":
            result = webhook_notify(updates, warnings=warnings or [])
            record_delivery("webhook", result)
        elif channel == "email":
            result = email_notify(updates, warnings=warnings or [])
            record_delivery("email", result)
        else:
            _config.log.warning(f"Unknown notification channel '{channel}' — skipping")
