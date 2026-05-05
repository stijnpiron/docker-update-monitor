"""Unit tests for email notifications and NOTIFY_CHANNELS dispatch."""

from unittest.mock import MagicMock, patch, call

import pytest

from app import config as config_mod
from app.models import UpdateInfo, RegexMismatch, ScanWarning
from app.notifications.email import (
    notify as email_notify,
    _build_html,
    _build_plain,
    _sort_updates,
    _split_by_status,
    _build_mismatch_section_html,
    _build_warnings_section_html,
)
from app.notifications import dispatch


def _make_update(**kwargs):
    defaults = dict(
        container_name="test-app",
        service_name="app",
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

    def test_groups_by_status(self):
        updates = [
            _make_update(stack="stack-a", container_name="app1", status="new"),
            _make_update(stack="stack-b", container_name="app2", status="known", image="redis"),
        ]
        html = _build_html(updates)
        assert "stack-a" in html
        assert "stack-b" in html
        assert "New updates" in html
        assert "Known updates" in html


class TestEmailBuildPlain:
    def test_contains_update_info(self):
        text = _build_plain([_make_update()])
        assert "nginx" in text
        assert "1.0.0" in text
        assert "1.1.0" in text
        assert "mystack" in text

    def test_groups_by_status(self):
        updates = [
            _make_update(stack="stack-a", container_name="app1", status="new"),
            _make_update(stack="stack-b", container_name="app2", status="known", image="redis"),
        ]
        text = _build_plain(updates)
        assert "[stack-a]" in text
        assert "[stack-b]" in text
        assert "New updates" in text
        assert "Known updates" in text


class TestEmailNotify:
    """Tests for the email notify function."""

    @patch("app.notifications.email.smtplib.SMTP")
    def test_sends_email_with_correct_headers(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

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
        assert "Docker Update Monitor" in args[2]
        assert "Subject:" in args[2]
        assert "image_update" in args[2]  # Q-encoded subject

    @patch("app.notifications.email.smtplib.SMTP")
    def test_multiple_recipients(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

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
        mock_smtp_cls.return_value = mock_server

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

    @patch("app.notifications.email.smtplib.SMTP_SSL")
    def test_port_465_uses_smtp_ssl(self, mock_smtp_ssl_cls):
        mock_server = MagicMock()
        mock_smtp_ssl_cls.return_value = mock_server

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 465), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", True), \
             patch.object(config_mod, "SMTP_USERNAME", "user"), \
             patch.object(config_mod, "SMTP_PASSWORD", "pass"):
            email_notify([_make_update()])

        mock_smtp_ssl_cls.assert_called_once_with("smtp.example.com", 465)
        mock_server.starttls.assert_not_called()
        mock_server.login.assert_called_once_with("user", "pass")
        mock_server.sendmail.assert_called_once()

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

        mock_smtp_cls.return_value.starttls.side_effect = Exception("connection refused")

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
        mock_smtp_cls.return_value = mock_server

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

    @patch("app.notifications.email.smtplib.SMTP")
    def test_subject_count_uses_deduped_total(self, mock_smtp_cls):
        """Subject count should match body count after dedup."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        updates = [
            _make_update(container_name="app1", image="nginx", update_type="minor", status="known"),
            _make_update(container_name="app2", image="nginx", update_type="minor", status="known"),
            _make_update(container_name="app3", image="redis", update_type="patch", status="known"),
        ]

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", False), \
             patch.object(config_mod, "SMTP_USERNAME", ""), \
             patch.object(config_mod, "SMTP_PASSWORD", ""):
            email_notify(updates)

        raw_email = mock_server.sendmail.call_args[0][2]
        # 3 updates across 3 different containers — no cross-container dedup, so total = 3
        assert "3_image_updates" in raw_email  # Q-encoded subject


class TestNotifyChannelsDispatch:
    """Tests for the channel dispatcher."""

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_webhook_only(self, mock_webhook, mock_email):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["webhook"]):
            dispatch(updates)
        mock_webhook.assert_called_once_with(updates, mismatches=[], warnings=[])
        mock_email.assert_not_called()

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_email_only(self, mock_webhook, mock_email):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["email"]):
            dispatch(updates)
        mock_email.assert_called_once_with(updates, mismatches=[], warnings=[])
        mock_webhook.assert_not_called()

    @patch("app.notifications.email_notify")
    @patch("app.notifications.webhook_notify")
    def test_both_channels(self, mock_webhook, mock_email):
        updates = [_make_update()]
        with patch.object(config_mod, "NOTIFY_CHANNELS", ["webhook", "email"]):
            dispatch(updates)
        mock_webhook.assert_called_once_with(updates, mismatches=[], warnings=[])
        mock_email.assert_called_once_with(updates, mismatches=[], warnings=[])

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


class TestSortUpdates:
    def test_sorts_by_stack_then_image(self):
        updates = [
            _make_update(stack="zstack", image="nginx"),
            _make_update(stack="astack", image="redis"),
            _make_update(stack="astack", image="nginx"),
        ]
        result = _sort_updates(updates)
        assert result[0].stack == "astack"
        assert result[0].image == "nginx"
        assert result[1].stack == "astack"
        assert result[1].image == "redis"
        assert result[2].stack == "zstack"


class TestSplitByStatus:
    def test_splits_all_three_statuses(self):
        updates = [
            _make_update(status="new"),
            _make_update(status="known"),
            _make_update(status="resolved"),
        ]
        new, known, resolved = _split_by_status(updates)
        assert len(new) == 1
        assert len(known) == 1
        assert len(resolved) == 1

    def test_empty_list(self):
        new, known, resolved = _split_by_status([])
        assert new == []
        assert known == []
        assert resolved == []


class TestBuildMismatchSectionHtml:
    def test_empty_returns_empty_string(self):
        assert _build_mismatch_section_html([]) == ""

    def test_contains_mismatch_info(self):
        html = _build_mismatch_section_html([_make_mismatch()])
        assert "Regex mismatches" in html
        assert "nginx" in html
        assert "latest" in html
        assert r"\d+" in html
        assert "mystack" in html
        assert "<table" in html

    def test_multiple_mismatches(self):
        mismatches = [
            _make_mismatch(container_name="app1", image="nginx"),
            _make_mismatch(container_name="app2", image="redis"),
        ]
        html = _build_mismatch_section_html(mismatches)
        assert "nginx" in html
        assert "redis" in html
        assert "(2)" in html


class TestBuildWarningsSectionHtml:
    def test_empty_returns_empty_string(self):
        assert _build_warnings_section_html([]) == ""

    def test_contains_warning_info(self):
        html = _build_warnings_section_html([_make_warning()])
        assert "Warnings" in html
        assert "Could not fetch tags" in html
        assert "test-app" in html
        assert "<table" in html

    def test_error_level_rendering(self):
        html = _build_warnings_section_html([_make_warning(level="error")])
        assert "#dc2626" in html  # error color

    def test_warning_level_rendering(self):
        html = _build_warnings_section_html([_make_warning(level="warning")])
        assert "#d97706" in html  # warning color


class TestBuildHtmlWithExtras:
    def test_html_includes_mismatches(self):
        html = _build_html(
            [_make_update()],
            mismatches=[_make_mismatch()],
        )
        assert "Regex mismatches" in html
        assert "nginx" in html

    def test_html_includes_warnings(self):
        html = _build_html(
            [_make_update()],
            warnings=[_make_warning()],
        )
        assert "Warnings" in html
        assert "Could not fetch tags" in html

    def test_html_resolved_section(self):
        html = _build_html([_make_update(status="resolved")])
        assert "Resolved" in html


class TestBuildPlainWithExtras:
    def test_plain_includes_mismatches(self):
        text = _build_plain(
            [_make_update()],
            mismatches=[_make_mismatch()],
        )
        assert "Regex mismatches" in text
        assert "pattern=" in text

    def test_plain_includes_warnings(self):
        text = _build_plain(
            [_make_update()],
            warnings=[_make_warning()],
        )
        assert "Warnings" in text
        assert "Could not fetch tags" in text

    def test_plain_resolved_section(self):
        text = _build_plain([_make_update(status="resolved")])
        assert "Resolved" in text

    def test_plain_warning_without_image(self):
        text = _build_plain(
            [_make_update()],
            warnings=[_make_warning(image="")],
        )
        assert "Could not fetch tags" in text


class TestNotifyWithMismatchesAndWarnings:
    @patch("app.notifications.email.smtplib.SMTP")
    def test_notify_sends_with_mismatches_only(self, mock_smtp_cls, caplog):
        import logging

        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", False), \
             patch.object(config_mod, "SMTP_USERNAME", ""), \
             patch.object(config_mod, "SMTP_PASSWORD", ""), \
             caplog.at_level(logging.INFO):
            email_notify([], mismatches=[_make_mismatch()])

        mock_server.sendmail.assert_called_once()
        assert "Email sent" in caplog.text

    @patch("app.notifications.email.smtplib.SMTP")
    def test_notify_sends_with_warnings_only(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", False), \
             patch.object(config_mod, "SMTP_USERNAME", ""), \
             patch.object(config_mod, "SMTP_PASSWORD", ""):
            email_notify([], warnings=[_make_warning()])

        mock_server.sendmail.assert_called_once()


class TestSubjectEdgeCases:
    @patch("app.notifications.email.smtplib.SMTP")
    def test_subject_singular_with_one_update(self, mock_smtp_cls):
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", False), \
             patch.object(config_mod, "SMTP_USERNAME", ""), \
             patch.object(config_mod, "SMTP_PASSWORD", ""):
            email_notify([_make_update()])

        raw_email = mock_server.sendmail.call_args[0][2]
        assert "1_image_update?=" in raw_email  # singular, Q-encoded

    @patch("app.notifications.email.smtplib.SMTP")
    def test_subject_zero_when_only_mismatches(self, mock_smtp_cls):
        """When only mismatches/warnings are sent, subject says '0 image updates' (plural)."""
        mock_server = MagicMock()
        mock_smtp_cls.return_value = mock_server

        with patch.object(config_mod, "SMTP_HOST", "smtp.example.com"), \
             patch.object(config_mod, "SMTP_PORT", 587), \
             patch.object(config_mod, "SMTP_FROM", "from@example.com"), \
             patch.object(config_mod, "SMTP_TO", ["to@example.com"]), \
             patch.object(config_mod, "SMTP_TLS", False), \
             patch.object(config_mod, "SMTP_USERNAME", ""), \
             patch.object(config_mod, "SMTP_PASSWORD", ""):
            email_notify([], mismatches=[_make_mismatch()])

        raw_email = mock_server.sendmail.call_args[0][2]
        assert "0_image_updates" in raw_email  # Q-encoded, plural for zero


class TestBuildRowsEdgeCases:
    def test_service_name_fallback_to_container_name(self):
        html = _build_html([_make_update(service_name=None, container_name="my-container")])
        # service_name is None, so container_name should appear in both Service and Container columns
        assert "my-container" in html

    def test_current_version_none_shows_dash(self):
        html = _build_html([_make_update(current_version=None)])
        assert "\u2014" in html  # em dash fallback

    def test_unknown_update_type_uses_gray_color(self):
        html = _build_html([_make_update(update_type="unknown")])
        assert "#6b7280" in html  # gray fallback color

    def test_plain_service_name_fallback(self):
        text = _build_plain([_make_update(service_name=None, container_name="my-container")])
        assert "my-container (my-container)" in text


class TestBuildHtmlDedup:
    def test_html_body_shows_all_containers_sharing_same_image(self):
        updates = [
            _make_update(container_name="app1", image="nginx", update_type="minor", new_version="1.1.0"),
            _make_update(container_name="app2", image="nginx", update_type="minor", new_version="1.1.0"),
            _make_update(container_name="app3", image="redis", update_type="patch", new_version="7.1.0"),
        ]
        html = _build_html(updates)
        # All 3 containers are distinct — no cross-container dedup
        assert "(3)" in html
        assert "app1" in html
        assert "app2" in html

    def test_plain_body_shows_all_containers_sharing_same_image(self):
        updates = [
            _make_update(container_name="app1", image="nginx", update_type="minor", new_version="1.1.0"),
            _make_update(container_name="app2", image="nginx", update_type="minor", new_version="1.1.0"),
            _make_update(container_name="app3", image="redis", update_type="patch", new_version="7.1.0"),
        ]
        text = _build_plain(updates)
        # All 3 containers are distinct — no cross-container dedup
        assert "New updates (3)" in text
        assert "app1" in text
        assert "app2" in text


class TestBuildWarningsHtmlEdgeCases:
    def test_warning_without_image_shows_dash(self):
        html = _build_warnings_section_html([_make_warning(image=None)])
        assert "\u2014" in html  # em dash fallback


class TestSplitByStatusEdgeCases:
    def test_unknown_status_is_silently_dropped(self):
        updates = [
            _make_update(status="new"),
            _make_update(status="unknown_status"),
        ]
        new, known, resolved = _split_by_status(updates)
        assert len(new) == 1
        assert len(known) == 0
        assert len(resolved) == 0


class TestMismatchServiceNameFallback:
    def test_html_uses_container_name_when_service_name_none(self):
        m = _make_mismatch(service_name=None, container_name="my-container")
        html = _build_mismatch_section_html([m])
        assert "my-container" in html
