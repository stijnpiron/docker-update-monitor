import re

import app.config as _config
import app.http as _http
from app.models import UpdateInfo, RegexMismatch, ScanWarning

_TELEGRAM_API = "https://api.telegram.org/bot{token}/sendMessage"
_MAX_MSG_LEN = 4096

# All MarkdownV2 special characters that must be escaped in regular text
_SPECIAL_TEXT = re.compile(r'([_*\[\]()~`>#+\-=|{}.!\\])')


def _esc(text: str) -> str:
    """Escape special chars for MarkdownV2 regular text."""
    return _SPECIAL_TEXT.sub(r'\\\1', str(text))


def _esc_code(text: str) -> str:
    """Escape for MarkdownV2 code entity — only ` and \\ need escaping."""
    return str(text).replace('\\', '\\\\').replace('`', '\\`')


def _build_lines(
    updates: list[UpdateInfo],
    mismatches: list[RegexMismatch],
    warnings: list[ScanWarning],
) -> list[str]:
    lines = ["*\U0001f433 Docker Updates Available*", ""]

    new = [u for u in updates if u.status == "new"]
    known = [u for u in updates if u.status == "known"]
    resolved = [u for u in updates if u.status == "resolved"]

    for emoji, title, group in [
        ("\U0001f195", "New", new),
        ("\U0001f504", "Known", known),
        ("✅", "Resolved", resolved),
    ]:
        if not group:
            continue
        lines.append(f"{emoji} *{_esc(title)} \\({len(group)}\\)*")
        for u in sorted(group, key=lambda x: (x.stack or "", x.image or "")):
            service = _esc(u.service_name or u.container_name)
            stack = _esc(u.stack)
            image = _esc(u.image or "")
            current = _esc_code(u.current_version or "—")
            new_ver = _esc_code(u.new_version)
            update_type = _esc(u.update_type)
            lines.append(f"• \\[{stack}\\] {service} \\({image}\\): `{current}` → `{new_ver}` \\({update_type}\\)")
        lines.append("")

    if mismatches:
        lines.append(f"⚠️ *Regex mismatches \\({len(mismatches)}\\)*")
        for m in mismatches:
            service = _esc(m.service_name or m.container_name)
            stack = _esc(m.stack)
            image = _esc(m.image)
            tag = _esc_code(m.current_tag)
            pattern = _esc_code(m.pattern)
            lines.append(f"• \\[{stack}\\] {service} \\({image}:`{tag}`\\) pattern: `{pattern}`")
        lines.append("")

    if warnings:
        lines.append(f"\U0001f6a8 *Warnings \\({len(warnings)}\\)*")
        for w in warnings:
            container = _esc(w.container_name)
            level = _esc(w.level.upper())
            message = _esc(w.message)
            lines.append(f"• \\[{level}\\] {container}: {message}")
        lines.append("")

    return lines


def _chunk_messages(lines: list[str]) -> list[str]:
    """Split lines into message chunks that fit within Telegram's 4096-char limit."""
    if not lines:
        return [""]
    chunks: list[str] = []
    current_parts: list[str] = []
    current_len = 0

    for line in lines:
        line_len = len(line) + 1  # +1 for newline
        if current_parts and current_len + line_len > _MAX_MSG_LEN:
            chunks.append("\n".join(current_parts))
            current_parts = []
            current_len = 0
        current_parts.append(line)
        current_len += line_len

    if current_parts:
        chunks.append("\n".join(current_parts))

    return chunks


def _send_message(token: str, chat_id: str, text: str) -> bool:
    url = _TELEGRAM_API.format(token=token)
    try:
        resp = _http.http_session.post(
            url,
            json={"chat_id": chat_id, "parse_mode": "MarkdownV2", "text": text},
            timeout=15,
        )
        resp.raise_for_status()
        return True
    except Exception as exc:
        _config.log.error(f"Failed to send Telegram message: {exc}")
        return False


def notify(
    updates: list[UpdateInfo],
    *,
    mismatches: list[RegexMismatch] | None = None,
    warnings: list[ScanWarning] | None = None,
) -> bool:
    if not updates and not mismatches and not warnings:
        return True

    if not _config.TELEGRAM_BOT_TOKEN or not _config.TELEGRAM_CHAT_ID:
        _config.log.error(
            "Telegram not configured: TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID are required."
        )
        return False

    lines = _build_lines(updates, mismatches or [], warnings or [])

    if _config.DRY_RUN:
        _config.log.info("DRY_RUN — would send Telegram message:\n" + "\n".join(lines))
        return True

    chunks = _chunk_messages(lines)
    success = True
    for chunk in chunks:
        if not _send_message(_config.TELEGRAM_BOT_TOKEN, _config.TELEGRAM_CHAT_ID, chunk):
            success = False

    if success:
        _config.log.info(
            f"Telegram notification sent ({len(chunks)} message(s), {len(updates)} update(s))"
        )

    return success
