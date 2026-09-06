import smtplib
from datetime import date
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from html import escape

import app.config as _config
from app.models import UpdateInfo, RegexMismatch, ScanWarning, HostStatusEvent

_TYPE_COLORS = {"major": "#dc2626", "minor": "#d97706", "patch": "#2563eb"}
_TD = "padding:6px 12px;border-bottom:1px solid #e5e7eb;"
_MONO = _TD + "font-family:monospace;font-size:13px;"


def _split_by_status(updates: list[UpdateInfo]) -> tuple[list[UpdateInfo], list[UpdateInfo], list[UpdateInfo]]:
    new, known, resolved = [], [], []
    for u in updates:
        if u.status == "new":
            new.append(u)
        elif u.status == "known":
            known.append(u)
        elif u.status == "resolved":
            resolved.append(u)
    return new, known, resolved


def _sort_updates(updates: list[UpdateInfo]) -> list[UpdateInfo]:
    # Rows group visually by host first (task 05), then stack, then image.
    return sorted(updates, key=lambda u: (u.host or "", u.stack or "", u.image or ""))


def _build_rows(updates: list[UpdateInfo]) -> str:
    rows = ""
    for u in updates:
        color = _TYPE_COLORS.get(u.update_type, "#6b7280")
        rows += (
            f'<tr>'
            f'<td style="{_TD}">{escape(u.host)}</td>'
            f'<td style="{_TD}font-weight:bold;">{escape(u.stack)}</td>'
            f'<td style="{_TD}">{escape(u.service_name or u.container_name)}</td>'
            f'<td style="{_TD}">{escape(u.container_name)}</td>'
            f'<td style="{_TD}">{escape(u.image)}</td>'
            f'<td style="{_MONO}">{escape(u.current_version or "\u2014")}</td>'
            f'<td style="{_MONO}color:{color};font-weight:bold;">{escape(u.new_version)}</td>'
            f'<td style="{_TD}color:{color};font-weight:bold;">{escape(u.update_type)}</td>'
            f'</tr>'
        )
    return rows


def _build_section(title: str, emoji: str, updates: list[UpdateInfo], header_color: str) -> str:
    if not updates:
        return ""
    return (
        f'<h3 style="color:{header_color};margin:20px 0 8px 0;">{emoji} {title} ({len(updates)})</h3>'
        f'<table style="border-collapse:collapse;width:100%;">'
        f'<thead><tr style="background:#f3f4f6;">'
        f'<th style="padding:6px 12px;text-align:left;">Host</th>'
        f'<th style="padding:6px 12px;text-align:left;">Stack</th>'
        f'<th style="padding:6px 12px;text-align:left;">Service</th>'
        f'<th style="padding:6px 12px;text-align:left;">Container</th>'
        f'<th style="padding:6px 12px;text-align:left;">Image</th>'
        f'<th style="padding:6px 12px;text-align:left;">Current</th>'
        f'<th style="padding:6px 12px;text-align:left;">Latest</th>'
        f'<th style="padding:6px 12px;text-align:left;">Type</th>'
        f'</tr></thead>'
        f'<tbody>{_build_rows(updates)}</tbody>'
        f'</table>'
    )


def _build_mismatch_section_html(mismatches: list[RegexMismatch]) -> str:
    if not mismatches:
        return ""
    rows = ""
    for m in mismatches:
        rows += (
            f'<tr>'
            f'<td style="{_TD}">{escape(m.host)}</td>'
            f'<td style="{_TD}font-weight:bold;">{escape(m.stack)}</td>'
            f'<td style="{_TD}">{escape(m.service_name or m.container_name)}</td>'
            f'<td style="{_TD}">{escape(m.container_name)}</td>'
            f'<td style="{_TD}">{escape(m.image)}</td>'
            f'<td style="{_MONO}">{escape(m.current_tag)}</td>'
            f'<td style="{_MONO}">{escape(m.pattern)}</td>'
            f'</tr>'
        )
    return (
        f'<h3 style="color:#6b7280;margin:20px 0 8px 0;">\u26a0\ufe0f Regex mismatches ({len(mismatches)})</h3>'
        f'<p style="color:#6b7280;font-size:13px;margin:0 0 8px 0;">'
        f'These containers have a tag-regex that does not match their current tag. Check your configuration.</p>'
        f'<table style="border-collapse:collapse;width:100%;">'
        f'<thead><tr style="background:#f3f4f6;">'
        f'<th style="padding:6px 12px;text-align:left;">Host</th>'
        f'<th style="padding:6px 12px;text-align:left;">Stack</th>'
        f'<th style="padding:6px 12px;text-align:left;">Service</th>'
        f'<th style="padding:6px 12px;text-align:left;">Container</th>'
        f'<th style="padding:6px 12px;text-align:left;">Image</th>'
        f'<th style="padding:6px 12px;text-align:left;">Current Tag</th>'
        f'<th style="padding:6px 12px;text-align:left;">Pattern</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table>'
    )


def _build_warnings_section_html(warnings: list[ScanWarning]) -> str:
    if not warnings:
        return ""
    rows = ""
    for w in warnings:
        color = "#dc2626" if w.level == "error" else "#d97706"
        rows += (
            f'<tr>'
            f'<td style="{_TD}color:{color};font-weight:bold;">{escape(w.level.upper())}</td>'
            f'<td style="{_TD}">{escape(w.host)}</td>'
            f'<td style="{_TD}">{escape(w.container_name)}</td>'
            f'<td style="{_TD}">{escape(w.image or "\u2014")}</td>'
            f'<td style="{_TD}">{escape(w.message)}</td>'
            f'</tr>'
        )
    return (
        f'<h3 style="color:#d97706;margin:20px 0 8px 0;">\U0001f6a8 Warnings &amp; errors ({len(warnings)})</h3>'
        f'<table style="border-collapse:collapse;width:100%;">'
        f'<thead><tr style="background:#f3f4f6;">'
        f'<th style="padding:6px 12px;text-align:left;">Level</th>'
        f'<th style="padding:6px 12px;text-align:left;">Host</th>'
        f'<th style="padding:6px 12px;text-align:left;">Container</th>'
        f'<th style="padding:6px 12px;text-align:left;">Image</th>'
        f'<th style="padding:6px 12px;text-align:left;">Message</th>'
        f'</tr></thead>'
        f'<tbody>{rows}</tbody>'
        f'</table>'
    )


def _build_html(updates: list[UpdateInfo], mismatches: list[RegexMismatch] | None = None, warnings: list[ScanWarning] | None = None) -> str:
    new, known, resolved = _split_by_status(updates)
    new = _sort_updates(new)
    known = _sort_updates(known)
    resolved = _sort_updates(resolved)

    sections = ""
    sections += _build_section("New updates", "\U0001f195", new, "#dc2626")
    sections += _build_section("Known updates", "\U0001f504", known, "#d97706")
    sections += _build_section("Resolved", "\u2705", resolved, "#16a34a")
    if mismatches:
        sections += _build_mismatch_section_html(mismatches)
    if warnings:
        sections += _build_warnings_section_html(warnings)

    today = date.today().strftime("%-d %B %Y")
    return (
        '<html><body style="font-family:sans-serif;color:#111;">'
        f'<h2 style="color:#1d4ed8;">Docker image updates \u2013 {today}</h2>'
        f'{sections}'
        '<p style="color:#6b7280;font-size:12px;margin-top:16px;">Sent by Docker Update Monitor</p>'
        '</body></html>'
    )


def _build_plain(updates: list[UpdateInfo], mismatches: list[RegexMismatch] | None = None, warnings: list[ScanWarning] | None = None) -> str:
    new, known, resolved = _split_by_status(updates)
    new = _sort_updates(new)
    known = _sort_updates(known)
    resolved = _sort_updates(resolved)

    lines = ["Docker Update Monitor", "=" * 40, ""]
    for title, group in [("New updates", new), ("Known updates", known), ("Resolved", resolved)]:
        if not group:
            continue
        lines.append(f"{title} ({len(group)})")
        lines.append("-" * 30)
        for u in group:
            lines.append(
                f"  [{u.host}] [{u.stack}] {u.service_name or u.container_name} ({u.container_name}) "
                f"{u.image} {u.current_version} -> {u.new_version} ({u.update_type})"
            )
        lines.append("")

    if mismatches:
        lines.append(f"Regex mismatches ({len(mismatches)})")
        lines.append("-" * 30)
        for m in mismatches:
            lines.append(
                f"  [{m.host}] [{m.stack}] {m.service_name or m.container_name} ({m.container_name}) "
                f"{m.image}:{m.current_tag}  pattern='{m.pattern}'"
            )
        lines.append("")

    if warnings:
        lines.append(f"Warnings ({len(warnings)})")
        lines.append("-" * 30)
        for w in warnings:
            image_part = f" {w.image}" if w.image else ""
            lines.append(f"  [{w.host}] [{w.level.upper()}] {w.container_name}{image_part}: {w.message}")
        lines.append("")

    return "\n".join(lines)


def notify(
    updates: list[UpdateInfo],
    *,
    mismatches: list[RegexMismatch] | None = None,
    warnings: list[ScanWarning] | None = None,
) -> bool | None:
    """Send email notification.

    Returns True on successful delivery, False on a delivery attempt that
    failed (e.g. SMTP error), and None when no delivery was attempted (empty
    payload, missing SMTP configuration).
    """
    if not updates and not mismatches and not warnings:
        return None

    if not _config.SMTP_HOST or not _config.SMTP_FROM or not _config.SMTP_TO:
        _config.log.warning("SMTP not fully configured (SMTP_HOST, SMTP_FROM, SMTP_TO required) — skipping email notification.")
        return None

    new, known, resolved = _split_by_status(updates)
    total = len(new) + len(known) + len(resolved)
    subject = f"\U0001f433 Docker Update Monitor \u2013 {total} image update{'s' if total != 1 else ''}"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _config.SMTP_FROM
    msg["To"] = ", ".join(_config.SMTP_TO)

    plain_body = _build_plain(updates, mismatches, warnings)
    html_body = _build_html(updates, mismatches, warnings)

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if _config.SMTP_PORT == 465:
            # Port 465: implicit SSL (SMTPS)
            server = smtplib.SMTP_SSL(_config.SMTP_HOST, _config.SMTP_PORT)
        else:
            # Port 587 or other: plain connect, then optional STARTTLS
            server = smtplib.SMTP(_config.SMTP_HOST, _config.SMTP_PORT)
            if _config.SMTP_TLS:
                server.starttls()

        with server:
            if _config.SMTP_USERNAME and _config.SMTP_PASSWORD:
                server.login(_config.SMTP_USERNAME, _config.SMTP_PASSWORD)
            server.sendmail(_config.SMTP_FROM, _config.SMTP_TO, msg.as_string())
    except Exception as exc:
        _config.log.error(f"Failed to send email notification: {exc}")
        return False

    _config.log.info(f"Email sent to {', '.join(_config.SMTP_TO)} with {len(updates)} update(s)")
    return True


def _event_text(event: HostStatusEvent) -> str:
    """Human-readable one-line summary of a single host up/down event."""
    if event.event == "recovered":
        return f"Host recovered: {event.host}"
    error = event.error or "unreachable"
    return f"Host unreachable: {event.host} — {error}"


def _build_host_status_bodies(event: HostStatusEvent) -> tuple[str, str, str]:
    """Build (subject, plain body, HTML body) for one host up/down event."""
    text = _event_text(event)
    if event.event == "recovered":
        emoji = "\u2705"
        color = "#16a34a"
    else:
        emoji = "\U0001f6a8"
        color = "#dc2626"

    subject = f"{emoji} Docker Update Monitor \u2013 {text}"

    plain = (
        "Docker Update Monitor\n"
        + "=" * 40
        + "\n\n"
        + text
        + "\n"
    )

    html = (
        '<html><body style="font-family:sans-serif;color:#111;">'
        f'<h2 style="color:{color};margin:0 0 12px 0;">{emoji} {escape(text)}</h2>'
        '<p style="color:#6b7280;font-size:12px;margin:16px 0 0 0;">'
        'Sent by Docker Update Monitor'
        '</p>'
        '</body></html>'
    )
    return subject, plain, html


def host_updown(event: HostStatusEvent) -> bool | None:
    """Send a host down/recovered email alert (task 05).

    Mirrors :func:`notify` return semantics:

    * ``None`` — no SMTP delivery was attempted (DRY_RUN, or incomplete SMTP
      configuration). The message is still logged in DRY_RUN mode.
    * ``False`` — a delivery attempt was made but failed (e.g. SMTP error).
    * ``True``  — the message was sent successfully.
    """
    if _config.DRY_RUN:
        _config.log.info("DRY_RUN — would send host-status email: " + _event_text(event))
        return None

    if not _config.SMTP_HOST or not _config.SMTP_FROM or not _config.SMTP_TO:
        _config.log.warning("SMTP not fully configured (SMTP_HOST, SMTP_FROM, SMTP_TO required) — skipping host-status notification.")
        return None

    subject, plain_body, html_body = _build_host_status_bodies(event)

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _config.SMTP_FROM
    msg["To"] = ", ".join(_config.SMTP_TO)

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        if _config.SMTP_PORT == 465:
            # Port 465: implicit SSL (SMTPS)
            server = smtplib.SMTP_SSL(_config.SMTP_HOST, _config.SMTP_PORT)
        else:
            # Port 587 or other: plain connect, then optional STARTTLS
            server = smtplib.SMTP(_config.SMTP_HOST, _config.SMTP_PORT)
            if _config.SMTP_TLS:
                server.starttls()

        with server:
            if _config.SMTP_USERNAME and _config.SMTP_PASSWORD:
                server.login(_config.SMTP_USERNAME, _config.SMTP_PASSWORD)
            server.sendmail(_config.SMTP_FROM, _config.SMTP_TO, msg.as_string())
    except Exception as exc:
        _config.log.error(f"Failed to send host-status email notification: {exc}")
        return False

    _config.log.info(f"Host-status email sent to {', '.join(_config.SMTP_TO)}: {_event_text(event)}")
    return True
