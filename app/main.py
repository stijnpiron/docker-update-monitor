import sys
import signal
import time
from datetime import datetime, timezone

from croniter import croniter

import app.config as _config
from app.scanner import run_check
from app.health import update_state
from app.state import load_last_check
from app.dashboard import start_dashboard, _scan_trigger

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

    # Restore last_check from persistent storage
    persisted_last_check = load_last_check()
    if persisted_last_check:
        from datetime import datetime as _dt
        try:
            lc = _dt.fromisoformat(persisted_last_check.replace("Z", "+00:00"))
            update_state(last_check=lc)
        except (ValueError, TypeError):
            pass

    if not croniter.is_valid(_config.CRON_SCHEDULE):
        _config.log.error(f"Invalid cron expression: '{_config.CRON_SCHEDULE}' — exiting")
        sys.exit(1)

    start_dashboard()

    _config.log.info(f"Schedule: '{_config.CRON_SCHEDULE}'")

    if _config.RUN_ON_STARTUP:
        _config.log.info("Running initial check on startup")
        run_check()
        if shutdown_requested:
            _config.log.info("Shutting down gracefully")
            sys.exit(0)

    cron = croniter(_config.CRON_SCHEDULE, datetime.now(timezone.utc))
    next_run = cron.get_next(datetime)
    update_state(next_check=next_run)
    _config.log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}")

    while not shutdown_requested:
        now = datetime.now(timezone.utc)
        wait = (next_run - now).total_seconds()

        # Sleep in 1-second intervals to allow prompt shutdown and scan triggers
        triggered = False
        while wait > 0 and not shutdown_requested:
            if _scan_trigger.is_set():
                _scan_trigger.clear()
                _config.log.info("Manual scan triggered via dashboard")
                run_check()
                triggered = True
                if shutdown_requested:
                    break
                # Recalculate next scheduled run from current time.
                # Do NOT call cron.get_next() here — that would advance the shared
                # iterator past the already-scheduled next_run, causing it to be skipped.
                # Instead, recreate the croniter from now so next_run is the first
                # scheduled time that is still in the future.
                cron = croniter(_config.CRON_SCHEDULE, datetime.now(timezone.utc))
                next_run = cron.get_next(datetime)
                update_state(next_check=next_run)
                _config.log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}\n")
                break
            time.sleep(min(wait, 1.0))
            wait -= 1.0

        if shutdown_requested:
            break

        # Run scheduled check if the sleep loop completed normally (not a manual trigger)
        if not triggered:
            run_check()

            if shutdown_requested:
                break

            next_run = cron.get_next(datetime)
            update_state(next_check=next_run)
            _config.log.info(f"Next check at: {next_run.strftime('%Y-%m-%dT%H:%M:%S %Z')}\n")

    _config.log.info("Shutting down gracefully")
    sys.exit(0)


if __name__ == "__main__":
    main()
