"""Unit tests for email notifications and NOTIFY_CHANNELS dispatch."""

from unittest.mock import MagicMock, patch, call

import pytest

from app import config as config_mod
from app.models import UpdateInfo
from app.notifications.email import notify as email_notify, _build_html, _build_plain
from app.notifications import dispatch


def _make_update(**kwargs):
    defaults = dict(
        container_name="test-app",
        stack="mystack",
        image="nginx",
        current_version="1.0.0",
        new_version="1.1.0",
        update_type="minor",
        status="new",
    )
    defaults.update(kwargs)
    return UpdateInfo(**defaults)


class TestEmailBuildHtml:
    def test_contains_table_with_update_info(self):
        html = _build_html([_make_update()])
        assert "<table" in html
        assert "nginx" in html
        assert "1.0.0" in html
        assert "1.1.0" in html
        assert "mystack" in html

    def test_groups_by_stack(self):
        updates = [
            _make_update(stack="stack-a", container_name="app1"),
            _make_update(stack="stack-b", container_name="app2"),
        ]
        html = _build_html(updates)
        assert "stack-a" in html
        assert "stack-b" in html


class TestEmailBuildPlain:
    def test_contains_update_info(self):
        text = _build_plain([_make_update()])
        assert "nginx" in text
        assert "1.0.0" in text
        assert "1.1.0" in text
        assert "mystack" in text

    def test_groups_by_stack(self):
        updates = [
            _make_update(stack="stack-a", container_name="app1"),
            _make_update(stack="stack-b", container_name="app2"),
        ]
        text = _build_plain(updates)
        assert "Stack: stack-a" in text
        assert "Stack: stack-b" in text


class TestEmailNotify:
    """Tests for the email notify function."""

    @patch("app.notifications.email.smtplib.SMTP")
    def test_sends_email_with_correct_headers(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", True), \
             patch.object(config_mod, "SMTP_USERNAME", "user"), \
             patch.object(config_mod, "SMTP_PASSWORD", "pass"):
            email_notify([_make_update()])

        mock_smtp_cls.assert_called_once_with("smtp.example.com", 587)
        mock_server.starttls.assert_called_once()
        mock_server.login.assert_called_once_with("user", "pass")
        mock_server.sendmail.assert_called_once()
        args = mock_server.sendmail.call_args[0]
        assert args[0] == "from@example.com"
        assert args[1] == ["to@example.com"]
        assert "[Docker Update Monitor]" in args[2]
        assert "1 new update(s)" in args[2]

    @patch("app.notifications.email.smtplib.SMTP")
    def test_multiple_recipients(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["a@example.com", "b@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", True), \
             patch.object(config_mod, "SMTP_USERNAME", "user"), \
             patch.object(config_mod, "SMTP_PASSWORD", "pass"):
            email_notify([_make_update()])

        args = mock_server.sendmail.call_args[0]
        assert args[1] == ["a@example.com", "b@example.com"]

    @patch("app.notifications.email.smtplib.SMTP")
    def test_no_tls_when_disabled(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 25), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", False), \
             patch.object(config_mod, "SMTP_USERNAME", ""), \
             patch.object(config_mod, "SMTP_PASSWORD", ""):
            email_notify([_make_update()])

        mock_server.starttls.assert_not_called()
        mock_server.login.assert_not_called()

    def test_missing_config_logs_warning(self, caplog):
        import logging

        with patch.object(config_mod, "SMTP_HOST", ""), \
             patch.object(config_mod, "SMTP_FROM", ""), \
             patch.object(config_mod, "SMTP_TO", []), \
             caplog.at_level(logging.WARNING):
            email_notify([_make_update()])

        assert "SMTP not fully configured" in caplog.text

    @patch("app.notifications.email.smtplib.SMTP")
    def test_smtp_error_logs_and_does_not_raise(self, mock_smtp_cls, caplog):
        import logging

        mock_smtp_cls.return_value.__enter__ = MagicMock(side_effect=Exception("connection refused"))
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", True), \
             patch.object(config_mod, "SMTP_USERNAME", "user"), \
             patch.object(config_mod, "SMTP_PASSWORD", "pass"), \
             caplog.at_level(logging.ERROR):
            email_notify([_make_update()])

        assert "Failed to send email" in caplog.text

    def test_empty_updates_does_nothing(self):
        with patch("app.notifications.email.smtplib.SMTP") as mock_smtp_cls:
            email_notify([])
        mock_smtp_cls.assert_not_called()

    @patch("app.notifications.email.smtplib.SMTP")
    def test_html_and_plain_parts_present(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value.__enter__ = MagicMock(return_value=mock_server)
        mock_smtp_cls.return_value.__exit__ = MagicMock(return_value=False)

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", True), \
             patch.object(config_mod, "SMTP_USERNAME", "user"), \
             patch.object(config_mod, "SMTP_PASSWORD", "pass"):
            email_notify([_make_update()])

        raw_email = mock_server.sendmail.call_args[0][2]
        assert "text/plain" in raw_email
        assert "text/html" in raw_email


class TestNotifyChannelsDispatch:
    """Tests for the channel dispatcher."""

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_webhook_only(self, mock_webhook, mock_email):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["webhook"]):
            dispatch(updates)
        mock_webhook.assert_called_once_with(updates)
        mock_email.assert_not_called()

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_email_only(self, mock_webhook, mock_email):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["email"]):
            dispatch(updates)
        mock_email.assert_called_once_with(updates)
        mock_webhook.assert_not_called()

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_both_channels(self, mock_webhook, mock_email):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["webhook", "email"]):
            dispatch(updates)
        mock_webhook.assert_called_once_with(updates)
        mock_email.assert_called_once_with(updates)

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_unknown_channel_logs_warning(self, mock_webhook, mock_email, caplog):
        import logging

        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["carrier_pigeon"]), \
             caplog.at_level(logging.WARNING):
            dispatch(updates)
        mock_webhook.assert_not_called()
        mock_email.assert_not_called()
        assert "Unknown notification channel" in caplog.text

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_empty_updates_dispatches_nothing(self, mock_webhook, mock_email):
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["webhook", "email"]):
            dispatch([])
        mock_webhook.assert_not_called()
        mock_email.assert_not_called()
