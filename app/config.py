import os
import logging

LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dum")


def _int_env(name: str, default: int) -> int:
    """Parse an integer env var, falling back to default with a warning if invalid."""
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError:
        log.warning("Invalid %s value %r, falling back to %d", name, raw, default)
        return default


NOTIFY_ENDPOINT   = os.environ.get("NOTIFY_ENDPOINT", "")
NOTIFY_AUTH_TYPE  = os.environ.get("NOTIFY_AUTH_TYPE", "").lower()
NOTIFY_AUTH_TOKEN = os.environ.get("NOTIFY_AUTH_TOKEN", "")
NOTIFY_CHANNELS   = [ch.strip() for ch in os.environ.get("NOTIFY_CHANNELS", "webhook").split(",") if ch.strip()]
DOCKERHUB_USER    = os.environ.get("DOCKERHUB_USERNAME", "")
DOCKERHUB_PASS    = os.environ.get("DOCKERHUB_PASSWORD", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
CRON_SCHEDULE     = os.environ.get("CRON_SCHEDULE", "0 * * * *")
RUN_ON_STARTUP    = os.environ.get("RUN_ON_STARTUP", "true").lower() == "true"
LABEL_PREFIX      = os.environ.get("LABEL_PREFIX", "docker-update-monitor")
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
STATE_DB_PATH     = os.environ.get("STATE_DB_PATH", "/app/data/state.db")

SMTP_HOST         = os.environ.get("SMTP_HOST", "")
SMTP_PORT         = _int_env("SMTP_PORT", 587)
SMTP_USERNAME     = os.environ.get("SMTP_USERNAME", "")
SMTP_PASSWORD     = os.environ.get("SMTP_PASSWORD", "")
SMTP_FROM         = os.environ.get("SMTP_FROM", "")
SMTP_TO           = [addr.strip() for addr in os.environ.get("SMTP_TO", "").split(",") if addr.strip()]
SMTP_TLS          = os.environ.get("SMTP_TLS", "true").lower() == "true"

WEB_PORT          = _int_env("WEB_PORT", 8080)
DASHBOARD_DATETIME_FORMAT = os.environ.get("DASHBOARD_DATETIME_FORMAT", "%d/%m/%Y %H:%M")
TZ                = os.environ.get("TZ", "")

UPDATE_COOLDOWN   = os.environ.get("UPDATE_COOLDOWN", "0")
