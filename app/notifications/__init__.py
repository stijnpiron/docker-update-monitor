import app.config as _config
from app.models import UpdateInfo
from app.notifications.webhook import notify as webhook_notify
from app.notifications.email import notify as email_notify


def dispatch(updates: list[UpdateInfo]) -> None:
    """Send notifications via all configured channels."""
    if not updates:
        return

    for channel in _config.NOTIFY_CHANNELS:
        if channel == "webhook":
            webhook_notify(updates)
        elif channel == "email":
            email_notify(updates)
        else:
            _config.log.warning(f"Unknown notification channel '{channel}' — skipping")
