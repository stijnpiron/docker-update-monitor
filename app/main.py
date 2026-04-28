import sys
import signal
import time
from datetime import datetime, timezone

from croniter import croniter

import app.config as _config
from app.scanner import run_check

shutdown_requested = False


def _handle_signal(signum, frame):
    global shutdown_requested
    shutdown_requested = True
    _config.log.info(f"Received signal {signum} — shutting down after current operation")


signal.signal(signal.SIGTERM, _handle_signal)
signal.signal(signal.SIGINT, _handle_signal)


def main() -> None:
    _config.log.info("Docker Update Monitor started")
    if _config.DRY_RUN:
        _config.log.info("DRY_RUN mode active — no HTTP POSTs will be made")

    if not croniter.is_valid(_config.CRON_SCHEDULE):
        _config.log.error(f"Invalid cron expression: '{_config.CRON_SCHEDULE}' — exiting")
        sys.exit(1)

    _config.log.info(f"Schedule: '{_config.CRON_SCHEDULE}'")

    if _config.RUN_ON_STARTUP:
        _config.log.info("Running initial check on startup")
        run_check()
        if shutdown_requested:
            _config.log.info("Shutting down gracefully")
            sys.exit(0)

    cron = croniter(_config.CRON_SCHEDULE, datetime.now(timezone.utc))
    next_run = cron.get_next(datetime)
    _config.log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}")

    while not shutdown_requested:
        now = datetime.now(timezone.utc)
        wait = (next_run - now).total_seconds()

        # Sleep in 1-second intervals to allow prompt shutdown
        while wait > 0 and not shutdown_requested:
            time.sleep(min(wait, 1.0))
            wait -= 1.0

        if shutdown_requested:
            break

        run_check()

        if shutdown_requested:
            break

        next_run = cron.get_next(datetime)
        _config.log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}\n")

    _config.log.info("Shutting down gracefully")
    sys.exit(0)


if __name__ == "__main__":
    main()
