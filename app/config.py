import os
import logging

NOTIFY_ENDPOINT   = os.environ.get("NOTIFY_ENDPOINT", "")
NOTIFY_AUTH_TYPE  = os.environ.get("NOTIFY_AUTH_TYPE", "").lower()
NOTIFY_AUTH_TOKEN = os.environ.get("NOTIFY_AUTH_TOKEN", "")
DOCKERHUB_USER    = os.environ.get("DOCKERHUB_USERNAME", "")
DOCKERHUB_PASS    = os.environ.get("DOCKERHUB_PASSWORD", "")
GITHUB_TOKEN      = os.environ.get("GITHUB_TOKEN", "")
CRON_SCHEDULE     = os.environ.get("CRON_SCHEDULE", "0 * * * *")
RUN_ON_STARTUP    = os.environ.get("RUN_ON_STARTUP", "true").lower() == "true"
LABEL_PREFIX      = os.environ.get("LABEL_PREFIX", "docker-update-monitor")
DRY_RUN           = os.environ.get("DRY_RUN", "false").lower() == "true"
STATE_DB_PATH     = os.environ.get("STATE_DB_PATH", "/app/data/state.db")
LOG_LEVEL         = os.environ.get("LOG_LEVEL", "INFO").upper()

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL, logging.INFO),
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%S",
)
log = logging.getLogger("dum")
