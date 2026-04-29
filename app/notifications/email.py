import smtplib
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from dataclasses import asdict

import app.config as _config
from app.models import UpdateInfo


def _group_by_stack(updates: list[UpdateInfo]) -> dict[str, list[UpdateInfo]]:
    grouped: dict[str, list[UpdateInfo]] = {}
    for u in updates:
        grouped.setdefault(u.stack, []).append(u)
    return grouped


def _build_html(updates: list[UpdateInfo]) -> str:
    grouped = _group_by_stack(updates)
    rows = ""
    for stack, items in grouped.items():
        for item in items:
            rows += (
                f"<tr>"
                f"<td>{stack}</td>"
                f"<td>{item.container_name}</td>"
                f"<td>{item.image}</td>"
                f"<td>{item.current_version}</td>"
                f"<td>{item.new_version}</td>"
                f"<td>{item.update_type}</td>"
                f"<td>{item.status}</td>"
                f"</tr>\n"
            )
    return (
        "<html><body>"
        "<h2>Docker Update Monitor</h2>"
        "<table border='1' cellpadding='4' cellspacing='0'>"
        "<tr><th>Stack</th><th>Container</th><th>Image</th>"
        "<th>Current</th><th>Available</th><th>Type</th><th>Status</th></tr>\n"
        f"{rows}"
        "</table>"
        "</body></html>"
    )


def _build_plain(updates: list[UpdateInfo]) -> str:
    grouped = _group_by_stack(updates)
    lines = ["Docker Update Monitor", "=" * 40, ""]
    for stack, items in grouped.items():
        lines.append(f"Stack: {stack}")
        for item in items:
            lines.append(
                f"  {item.container_name}: {item.image} "
                f"{item.current_version} -> {item.new_version} "
                f"({item.update_type}, {item.status})"
            )
        lines.append("")
    return "\n".join(lines)


def notify(updates: list[UpdateInfo]) -> None:
    if not updates:
        return

    if not _config.SMTP_HOST or not _config.SMTP_FROM or not _config.SMTP_TO:
        _config.log.warning("SMTP not fully configured (SMTP_HOST, SMTP_FROM, SMTP_TO required) — skipping email notification.")
        return

    subject = f"[Docker Update Monitor] {len(updates)} new update(s) available"

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = _config.SMTP_FROM
    msg["To"] = ", ".join(_config.SMTP_TO)

    plain_body = _build_plain(updates)
    html_body = _build_html(updates)

    msg.attach(MIMEText(plain_body, "plain"))
    msg.attach(MIMEText(html_body, "html"))

    try:
        with smtplib.SMTP(_config.SMTP_HOST, _config.SMTP_PORT) as server:
            if _config.SMTP_TLS:
                server.starttls()
            if _config.SMTP_USERNAME and _config.SMTP_PASSWORD:
                server.login(_config.SMTP_USERNAME, _config.SMTP_PASSWORD)
            server.sendmail(_config.SMTP_FROM, _config.SMTP_TO, msg.as_string())
        _config.log.info(f"Email sent to {', '.join(_config.SMTP_TO)} with {len(updates)} update(s)")
    except Exception as exc:
        _config.log.error(f"Failed to send email notification: {exc}")
