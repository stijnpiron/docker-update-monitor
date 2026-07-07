"""Tests for env-var parsing in app.config (issue #121)."""

import importlib
import logging

import pytest

import app.config as config_mod


def _reload_config(monkeypatch, **env):
    """Reload app.config with the given env vars overriding os.environ."""
    for key, value in env.items():
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    return importlib.reload(config_mod)


@pytest.fixture
def restore_config():
    """Reload config back to current process env after each test."""
    yield
    importlib.reload(config_mod)


class TestIntEnvHelper:
    def test_returns_default_when_unset(self, monkeypatch):
        monkeypatch.delenv("SOME_INT", raising=False)
        assert config_mod._int_env("SOME_INT", 42) == 42

    def test_returns_default_when_empty(self, monkeypatch):
        monkeypatch.setenv("SOME_INT", "")
        assert config_mod._int_env("SOME_INT", 42) == 42

    def test_parses_valid_int(self, monkeypatch):
        monkeypatch.setenv("SOME_INT", "1234")
        assert config_mod._int_env("SOME_INT", 42) == 1234

    def test_invalid_int_falls_back_with_warning(self, monkeypatch, caplog):
        monkeypatch.setenv("SOME_INT", "not_a_number")
        with caplog.at_level(logging.WARNING, logger="dum"):
            result = config_mod._int_env("SOME_INT", 42)
        assert result == 42
        assert any(
            "Invalid SOME_INT" in record.getMessage() and "not_a_number" in record.getMessage()
            for record in caplog.records
        )


class TestSmtpPort:
    def test_default_when_unset(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, SMTP_PORT=None)
        assert cfg.SMTP_PORT == 587

    def test_valid_value(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, SMTP_PORT="2525")
        assert cfg.SMTP_PORT == 2525

    def test_invalid_value_falls_back(self, monkeypatch, caplog, restore_config):
        with caplog.at_level(logging.WARNING, logger="dum"):
            cfg = _reload_config(monkeypatch, SMTP_PORT="not_a_number")
        assert cfg.SMTP_PORT == 587
        assert any("SMTP_PORT" in record.getMessage() for record in caplog.records)

    def test_does_not_raise_on_invalid(self, monkeypatch, restore_config):
        # The bug was a crash at import time; reloading must not raise.
        _reload_config(monkeypatch, SMTP_PORT="abc")


class TestWebPort:
    def test_default_when_unset(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, WEB_PORT=None)
        assert cfg.WEB_PORT == 8080

    def test_valid_value(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, WEB_PORT="9090")
        assert cfg.WEB_PORT == 9090

    def test_invalid_value_falls_back(self, monkeypatch, caplog, restore_config):
        with caplog.at_level(logging.WARNING, logger="dum"):
            cfg = _reload_config(monkeypatch, WEB_PORT="oops")
        assert cfg.WEB_PORT == 8080
        assert any("WEB_PORT" in record.getMessage() for record in caplog.records)

    def test_does_not_raise_on_invalid(self, monkeypatch, restore_config):
        _reload_config(monkeypatch, WEB_PORT="oops")
