"""Unit tests for the Telegram notification channel."""

import logging
from unittest.mock import MagicMock, patch

import pytest

import app.config as config_mod
from app.models import UpdateInfo, RegexMismatch, ScanWarning
from app.notifications.telegram import (
    notify,
    _esc,
    _esc_code,
    _build_lines,
    _chunk_messages,
    _send_message,
)
from app.notifications import dispatch


def _make_update(**kwargs):
    defaults = dict(
        container_name="test-app",
        service_name="app",
        stack="mystack",
        image="nginx",
        current_version="1.25",
        new_version="1.27",
        update_type="minor",
        status="new",
    )
    defaults.update(kwargs)
    return UpdateInfo(**defaults)


def _make_mismatch(**kwargs):
    defaults = dict(
        container_name="test-app",
        service_name="app",
        stack="mystack",
        image="nginx",
        current_tag="latest",
        pattern=r"^\d+\.\d+\.\d+$",
        reason="did not match current tag",
    )
    defaults.update(kwargs)
    return RegexMismatch(**defaults)


def _make_warning(**kwargs):
    defaults = dict(
        container_name="test-app",
        image="nginx",
        level="warning",
        message="Could not fetch tags",
    )
    defaults.update(kwargs)
    return ScanWarning(**defaults)


class TestEscapeText:
    def test_escapes_dot(self):
        assert _esc("1.25") == r"1\.25"

    def test_escapes_parens(self):
        assert _esc("(minor)") == r"\(minor\)"

    def test_escapes_brackets(self):
        assert _esc("[stack]") == r"\[stack\]"

    def test_escapes_underscore(self):
        assert _esc("my_stack") == r"my\_stack"

    def test_escapes_asterisk(self):
        assert _esc("a*b") == r"a\*b"

    def test_escapes_backslash(self):
        assert _esc("a\\b") == r"a\\b"

    def test_escapes_hyphen(self):
        assert _esc("my-stack") == r"my\-stack"

    def test_plain_text_unchanged(self):
        assert _esc("nginx") == "nginx"

    def test_empty_string(self):
        assert _esc("") == ""


class TestEscapeCode:
    def test_does_not_escape_dot(self):
        assert _esc_code("1.25") == "1.25"

    def test_escapes_backtick(self):
        assert _esc_code("a`b") == "a\\`b"

    def test_escapes_backslash(self):
        assert _esc_code("a\\b") == "a\\\\b"

    def test_plain_text_unchanged(self):
        assert _esc_code("1.25.3") == "1.25.3"


class TestBuildLines:
    def test_header_present(self):
        lines = _build_lines([_make_update()], [], [])
        assert lines[0] == "*\U0001f433 Docker Updates Available*"

    def test_new_update_included(self):
        lines = _build_lines([_make_update(status="new")], [], [])
        text = "\n".join(lines)
        assert "New" in text
        assert "nginx" in text
        assert "1.25" in text  # unescaped in code context
        assert "1.27" in text

    def test_known_update_included(self):
        lines = _build_lines([_make_update(status="known")], [], [])
        text = "\n".join(lines)
        assert "Known" in text

    def test_resolved_update_included(self):
        lines = _build_lines([_make_update(status="resolved")], [], [])
        text = "\n".join(lines)
        assert "Resolved" in text

    def test_empty_status_groups_omitted(self):
        lines = _build_lines([_make_update(status="new")], [], [])
        text = "\n".join(lines)
        assert "Known" not in text
        assert "Resolved" not in text

    def test_mismatches_included(self):
        lines = _build_lines([], [_make_mismatch()], [])
        text = "\n".join(lines)
        assert "Regex mismatches" in text
        assert "mystack" in text

    def test_warnings_included(self):
        lines = _build_lines([], [], [_make_warning()])
        text = "\n".join(lines)
        assert "Warnings" in text
        assert "Could not fetch tags" in text

    def test_update_sorted_by_stack_then_image(self):
        updates = [
            _make_update(stack="z-stack", image="redis", status="new"),
            _make_update(stack="a-stack", image="nginx", status="new"),
        ]
        lines = _build_lines(updates, [], [])
        text = "\n".join(lines)
        a_pos = text.index("a\\-stack")
        z_pos = text.index("z\\-stack")
        assert a_pos < z_pos

    def test_special_chars_in_stack_escaped(self):
        lines = _build_lines([_make_update(stack="my.stack")], [], [])
        text = "\n".join(lines)
        assert r"my\.stack" in text

    def test_service_name_fallback_to_container(self):
        lines = _build_lines([_make_update(service_name=None, container_name="my-container")], [], [])
        text = "\n".join(lines)
        assert "my\\-container" in text

    def test_current_version_none_uses_em_dash(self):
        lines = _build_lines([_make_update(current_version=None)], [], [])
        text = "\n".join(lines)
        assert "—" in text

    def test_count_in_section_header(self):
        updates = [_make_update(status="new"), _make_update(status="new", image="redis")]
        lines = _build_lines(updates, [], [])
        text = "\n".join(lines)
        assert r"\(2\)" in text


class TestChunkMessages:
    def test_short_message_is_single_chunk(self):
        lines = ["line1", "line2", "line3"]
        chunks = _chunk_messages(lines)
        assert len(chunks) == 1
        assert "line1" in chunks[0]
        assert "line3" in chunks[0]

    def test_long_message_splits_into_multiple_chunks(self):
        # Create lines that total >4096 chars
        lines = ["x" * 100] * 50  # 50 * 101 chars = 5050 chars
        chunks = _chunk_messages(lines)
        assert len(chunks) > 1
        for chunk in chunks:
            assert len(chunk) <= 4096

    def test_empty_lines_single_empty_chunk(self):
        chunks = _chunk_messages([])
        assert chunks == [""]

    def test_each_chunk_within_limit(self):
        lines = ["a" * 200] * 25
        chunks = _chunk_messages(lines)
        for chunk in chunks:
            assert len(chunk) <= 4096


class TestSendMessage:
    def test_returns_true_on_success(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"):
            result = _send_message("tok123", "-100123", "hello")

        assert result is True
        mock_http_session.post.assert_called_once()
        call_kwargs = mock_http_session.post.call_args
        assert "tok123" in call_kwargs[0][0]
        assert call_kwargs[1]["json"]["chat_id"] == "-100123"
        assert call_kwargs[1]["json"]["parse_mode"] == "MarkdownV2"

    def test_returns_false_and_logs_on_http_error(self, mock_http_session, caplog):
        mock_http_session.post.side_effect = Exception("network error")

        with caplog.at_level(logging.ERROR):
            result = _send_message("tok", "chat", "text")

        assert result is False
        assert "Failed to send Telegram message" in caplog.text

    def test_returns_false_on_bad_status(self, mock_http_session, caplog):
        from requests import HTTPError

        mock_resp = MagicMock()
        mock_resp.raise_for_status.side_effect = HTTPError("400 Bad Request")
        mock_http_session.post.return_value = mock_resp

        with caplog.at_level(logging.ERROR):
            result = _send_message("tok", "chat", "text")

        assert result is False
        assert "Failed to send Telegram message" in caplog.text


class TestNotify:
    def test_empty_updates_returns_true_without_http(self, mock_http_session):
        result = notify([])
        assert result is True
        mock_http_session.post.assert_not_called()

    def test_missing_token_logs_error_and_returns_false(self, mock_http_session, caplog):
        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"), \
             caplog.at_level(logging.ERROR):
            result = notify([_make_update()])

        assert result is False
        assert "TELEGRAM_BOT_TOKEN" in caplog.text
        mock_http_session.post.assert_not_called()

    def test_missing_chat_id_logs_error_and_returns_false(self, mock_http_session, caplog):
        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", ""), \
             caplog.at_level(logging.ERROR):
            result = notify([_make_update()])

        assert result is False
        assert "TELEGRAM_CHAT_ID" in caplog.text
        mock_http_session.post.assert_not_called()

    def test_both_config_missing_logs_error(self, mock_http_session, caplog):
        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", ""), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", ""), \
             caplog.at_level(logging.ERROR):
            result = notify([_make_update()])

        assert result is False
        mock_http_session.post.assert_not_called()

    def test_successful_send_returns_true(self, mock_http_session, caplog):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"), \
             caplog.at_level(logging.INFO):
            result = notify([_make_update()])

        assert result is True
        mock_http_session.post.assert_called_once()
        assert "Telegram notification sent" in caplog.text

    def test_failed_send_returns_false(self, mock_http_session, caplog):
        mock_http_session.post.side_effect = Exception("timeout")

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"), \
             caplog.at_level(logging.ERROR):
            result = notify([_make_update()])

        assert result is False
        assert "Failed to send Telegram message" in caplog.text

    def test_dry_run_logs_and_skips_http(self, mock_http_session, caplog):
        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"), \
             patch.object(config_mod, "DRY_RUN", True), \
             caplog.at_level(logging.INFO):
            result = notify([_make_update()])

        assert result is True
        mock_http_session.post.assert_not_called()
        assert "DRY_RUN" in caplog.text

    def test_only_mismatches_triggers_send(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"):
            result = notify([], mismatches=[_make_mismatch()])

        assert result is True
        mock_http_session.post.assert_called_once()

    def test_only_warnings_triggers_send(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"):
            result = notify([], warnings=[_make_warning()])

        assert result is True
        mock_http_session.post.assert_called_once()

    def test_long_update_list_sends_multiple_messages(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        # Generate enough updates to exceed 4096 chars
        updates = [
            _make_update(
                container_name=f"container-{i}",
                service_name=f"service-{i}",
                stack=f"stack-{i}",
                image=f"image-with-a-long-name-{i}",
                current_version="1.0.0",
                new_version="2.0.0",
                status="new",
            )
            for i in range(60)
        ]

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"):
            result = notify(updates)

        assert result is True
        assert mock_http_session.post.call_count > 1

    def test_partial_failure_returns_false(self, mock_http_session, caplog):
        ok_resp = MagicMock()
        ok_resp.raise_for_status.return_value = None
        fail_side_effect = Exception("send failed")

        call_count = 0

        def side_effect(*args, **kwargs):
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                return ok_resp
            raise fail_side_effect

        mock_http_session.post.side_effect = side_effect

        updates = [
            _make_update(
                container_name=f"c{i}",
                service_name=f"s{i}",
                stack=f"st{i}",
                image=f"img-{i}",
                status="new",
            )
            for i in range(90)
        ]

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"), \
             caplog.at_level(logging.ERROR):
            result = notify(updates)

        assert result is False

    def test_post_uses_markdownv2_parse_mode(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"):
            notify([_make_update()])

        call_kwargs = mock_http_session.post.call_args[1]
        assert call_kwargs["json"]["parse_mode"] == "MarkdownV2"

    def test_post_url_contains_bot_token(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "my-secret-token"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-100123"):
            notify([_make_update()])

        url = mock_http_session.post.call_args[0][0]
        assert "my-secret-token" in url

    def test_post_uses_correct_chat_id(self, mock_http_session):
        mock_resp = MagicMock()
        mock_resp.raise_for_status.return_value = None
        mock_http_session.post.return_value = mock_resp

        with patch.object(config_mod, "TELEGRAM_BOT_TOKEN", "tok123"), \
             patch.object(config_mod, "TELEGRAM_CHAT_ID", "-9991234"):
            notify([_make_update()])

        call_kwargs = mock_http_session.post.call_args[1]
        assert call_kwargs["json"]["chat_id"] == "-9991234"


class TestDispatchTelegramChannel:
    @patch("app.notifications.telegram_notify")
    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_telegram_channel_dispatched(self, mock_webhook, mock_email, mock_telegram):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["telegram"]):
            dispatch(updates)

        mock_telegram.assert_called_once_with(updates, mismatches=[], warnings=[])
        mock_webhook.assert_not_called()
        mock_email.assert_not_called()

    @patch("app.notifications.telegram_notify")
    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_telegram_with_other_channels(self, mock_webhook, mock_email, mock_telegram):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["webhook", "telegram"]):
            dispatch(updates)

        mock_webhook.assert_called_once_with(updates, mismatches=[], warnings=[])
        mock_telegram.assert_called_once_with(updates, mismatches=[], warnings=[])
        mock_email.assert_not_called()
