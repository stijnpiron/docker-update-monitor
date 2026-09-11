import os
import re
import sys
import logging

from datetime import timedelta

from app.cooldown import parse_cooldown

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

# Raw env-var string the scanner parses (per-container label fallback + the
# per-scan global cooldown). Normalized at import below (QA I5).
UPDATE_COOLDOWN_RAW = os.environ.get("UPDATE_COOLDOWN", "0")

# Valid host names: alphanumeric plus dot, underscore, and hyphen.
_HOST_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

_DEFAULT_HOST_REACH_COOLDOWN = "1h"


def _parse_cooldown_env(name: str, raw: str, default: str) -> str:
    """Return *raw* if it parses as a cooldown, else *default* with a warning.

    Shared by ``UPDATE_COOLDOWN`` and ``HOST_REACH_COOLDOWN`` (QA I5) so a typo
    in either never raises out of startup / ``run_check()`` — both fall back
    to a safe default with a logged warning.
    """
    try:
        parse_cooldown(raw)
        return raw
    except ValueError:
        log.warning("Invalid %s value %r, falling back to %s", name, raw, default)
        return default


UPDATE_COOLDOWN_RAW = _parse_cooldown_env("UPDATE_COOLDOWN", UPDATE_COOLDOWN_RAW, "0")


def _fail_host_config(message: str) -> None:
    """Log a host-config validation error and exit, matching invalid-CRON behavior.

    Used for both ``DOCKER_HOSTS`` and ``LOCAL_HOST_NAME`` — the two env vars
    share the same validation ruleset (name charset, uniqueness, reserved words).
    """
    log.error(f"Invalid host-name configuration — {message} — exiting")
    sys.exit(1)


# Display name for the local Docker daemon (the entry with url ``None``).
# Default "local"; can be renamed via the env var of the same name.
# Must match ``_HOST_NAME_RE`` (same rule as ``DOCKER_HOSTS`` names) so the
# local host can share the same badge/color/sort rules as remote hosts. Empty
# after ``.strip()`` falls back to "local" silently (a stray whitespace is not
# a config error — it's the kind of thing a hand-edited .env produces). Invalid
# content (non-ASCII, spaces, slashes, …) is a hard fail-fast at startup — the
# same pattern as an invalid ``CRON_SCHEDULE`` or a bad ``DOCKER_HOSTS`` value,
# so the typo surfaces as a logged error rather than a "host unreachable"
# row on the first scan.
LOCAL_HOST_NAME = os.environ.get("LOCAL_HOST_NAME", "local").strip() or "local"
if not _HOST_NAME_RE.match(LOCAL_HOST_NAME):
    _fail_host_config(
        f"LOCAL_HOST_NAME value {LOCAL_HOST_NAME!r} contains invalid characters "
        "(allowed: A-Za-z0-9 . _ -)"
    )


def _parse_docker_hosts() -> list[tuple[str, str | None]]:
    """Parse the ``DOCKER_HOSTS`` env var into ordered ``(name, url)`` tuples.

    The local daemon (url ``None``) is always first, named ``LOCAL_HOST_NAME``
    (default ``"local"``); every remote host is ``ssh://…``. Exits with a clear
    message on any validation failure rather than silently dropping a bad value.
    """
    hosts: list[tuple[str, str | None]] = [(LOCAL_HOST_NAME, None)]
    raw = os.environ.get("DOCKER_HOSTS")
    if raw is None or raw.strip() == "":
        return hosts

    for segment in raw.split(","):
        segment = segment.strip()
        if not segment:
            continue

        if "=" not in segment:
            _fail_host_config(
                f"host entry {segment!r} is missing '=' (expected name=ssh://…)"
            )

        name, _, url = segment.partition("=")
        name = name.strip()
        url = url.strip()

        if not name:
            _fail_host_config(f"host entry {segment!r} has an empty name")

        if name == LOCAL_HOST_NAME:
            _fail_host_config(
                f"host entry {segment!r} uses the reserved local host name "
                f"{LOCAL_HOST_NAME!r} (set via LOCAL_HOST_NAME; rename the "
                "remote, or choose a different LOCAL_HOST_NAME)"
            )

        if not _HOST_NAME_RE.match(name):
            _fail_host_config(
                f"host name {name!r} contains invalid characters "
                "(allowed: A-Za-z0-9 . _ -)"
            )

        if not url.startswith("ssh://"):
            _fail_host_config(
                f"host {name!r} has docker_host_url {url!r} but only the "
                "ssh:// scheme is supported"
            )

        hosts.append((name, url))

    if len({h[0] for h in hosts}) != len(hosts):
        _fail_host_config("duplicate host name in DOCKER_HOSTS")

    return hosts


DOCKER_HOSTS          = _parse_docker_hosts()
HOST_REACH_COOLDOWN   = parse_cooldown(_parse_cooldown_env(
    "HOST_REACH_COOLDOWN",
    os.environ.get("HOST_REACH_COOLDOWN") or _DEFAULT_HOST_REACH_COOLDOWN,
    _DEFAULT_HOST_REACH_COOLDOWN,
))
