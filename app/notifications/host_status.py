"""Host up/down notifications (task 05).

Host reachability transitions are *not* ``UpdateInfo`` rows, so they do not
travel through the update-oriented ``dispatch()``. Instead the scanner calls
:func:`notify_host_status` with the list of transitions it detected since the
previous scan ("down" when a previously-reachable host became unreachable,
"recovered" when a previously-unreachable host came back).

Coalescing
----------

The existing cooldown is per *container update row*; it cannot gate host
events. Instead these alerts use the ``event_cooldowns`` table (task 03):
a down/recovered alert for host *X* fires only if more than
``config.HOST_REACH_COOLDOWN`` has elapsed since the event identified by
``"host:<X>"`` last fired. ``last_fired_at`` is set **only when a
notification is actually dispatched** — in DRY_RUN or with no usable
channel the cooldown is not consumed, so the alert is not silently lost.

DRY_RUN / no channels never raise; they only suppress the outbound dispatch.
Transitions are always logged here (and by the scanner itself when it
records reachability).
"""

from datetime import datetime, timezone

import app.config as _config
from app.models import HostStatusEvent
from app.notifications.webhook import host_updown as webhook_host_updown
from app.notifications.email import host_updown as email_host_updown
from app.state import get_event_last_fired, set_event_last_fired
from app.metrics import notifications_attempted_total, notifications_sent_total


def _active_channels() -> list[str]:
    """Channels that will actually deliver a host-status alert for this run.

    A channel is active when it is configured and capable of delivering:
    webhook requires a ``NOTIFY_ENDPOINT``, email requires a full SMTP
    configuration. DRY_RUN excludes every channel, because nothing is sent
    outbound in dry-run mode (mirroring the update notification semantics).
    """
    if _config.DRY_RUN:
        return []
    active = []
    for channel in _config.NOTIFY_CHANNELS:
        if channel == "webhook" and _config.NOTIFY_ENDPOINT:
            active.append("webhook")
        elif channel == "email" and _config.SMTP_HOST and _config.SMTP_FROM and _config.SMTP_TO:
            active.append("email")
    return active


def notify_host_status(
    events: list[HostStatusEvent],
    *,
    now: datetime | None = None,
) -> None:
    """Dispatch host down/recovered alerts for *events*, coalesced per host.

    Returns silently when *events* is empty. For each event:
    * the transition is logged (always, even when suppressed — AC7);
    * an alert is sent over every active channel — but only when the
      ``HOST_REACH_COOLDOWN`` window has elapsed since the last alert for
      this host (or no alert has ever fired for it);
    * ``last_fired_at`` for key ``"host:<name>"`` is set only when at least
      one channel actually received a dispatch attempt (AC3/AC5).
    """
    if not events:
        return

    if now is None:
        now = datetime.now(timezone.utc)

    active = _active_channels()
    cooldown = _config.HOST_REACH_COOLDOWN

    for event in events:
        key = f"host:{event.host}"
        message = (
            f"Host down: {event.host} — {event.error or 'unreachable'}"
            if event.event == "down"
            else f"Host recovered: {event.host}"
        )
        _config.log.info(message)

        if not active:
            # DRY_RUN or no usable channel: log only, never consume the
            # cooldown, so a persistent outage is not lost.
            continue

        last_fired = get_event_last_fired(key)
        if last_fired is not None:
            try:
                if now - datetime.fromisoformat(last_fired) < cooldown:
                    _config.log.info(
                        f"  Host {event.host} alert in cooldown "
                        f"({cooldown}), suppressing"
                    )
                    continue
            except ValueError:
                # Unparseable stored timestamp — treat as never fired.
                pass

        set_event_last_fired(key, now.isoformat())
        for channel in active:
            try:
                if channel == "webhook":
                    result = webhook_host_updown(event)
                else:
                    result = email_host_updown(event)
            except Exception as exc:  # a broken channel must not break the scan
                _config.log.error(f"Host-status {channel} notification failed: {exc}")
                continue
            _record_message_delivery(channel, result)


def _record_message_delivery(channel: str, result: bool | None) -> None:
    """Update attempted/sent counters for a host-status message.

    Mirrors ``_record`` in ``app/notifications/__init__.py``: ``None`` means
    the notifier skipped (no dispatch attempted).
    """
    if result is None:
        return
    notifications_attempted_total.labels(channel=channel).inc()
    if result:
        notifications_sent_total.labels(channel=channel).inc()
