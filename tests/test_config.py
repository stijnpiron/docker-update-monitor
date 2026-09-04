"""Tests for env-var parsing in app.config (issue #121)."""

import importlib
import logging
import os
from datetime import timedelta

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
    # An invalid DOCKER_HOSTS value raises SystemExit at import time, so clear
    # it before reloading to avoid corrupting module state for later tests.
    os.environ.pop("DOCKER_HOSTS", None)
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


class TestDockerHosts:
    def test_hosts_unset_defaults_to_local(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, DOCKER_HOSTS=None)
        assert cfg.DOCKER_HOSTS == [("local", None)]

    def test_hosts_empty_defaults_to_local(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, DOCKER_HOSTS="  ")
        assert cfg.DOCKER_HOSTS == [("local", None)]

    def test_hosts_single_pair_appended_after_local(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, DOCKER_HOSTS="prod=ssh://monitor@prod-host")
        assert cfg.DOCKER_HOSTS == [("local", None), ("prod", "ssh://monitor@prod-host")]

    def test_hosts_multiple_pairs_preserve_order(self, monkeypatch, restore_config):
        cfg = _reload_config(
            monkeypatch,
            DOCKER_HOSTS="prod=ssh://a, staging=ssh://b, edge=ssh://c",
        )
        assert cfg.DOCKER_HOSTS == [
            ("local", None),
            ("prod", "ssh://a"),
            ("staging", "ssh://b"),
            ("edge", "ssh://c"),
        ]

    def test_hosts_whitespace_and_empty_segment_tolerated(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, DOCKER_HOSTS=" prod = ssh://a ,  ,staging=ssh://b,")
        assert cfg.DOCKER_HOSTS == [
            ("local", None),
            ("prod", "ssh://a"),
            ("staging", "ssh://b"),
        ]

    def test_hosts_rejects_missing_equals(self, monkeypatch, restore_config):
        with pytest.raises(SystemExit) as exc:
            _reload_config(monkeypatch, DOCKER_HOSTS="just-a-name")
        assert exc.value.code == 1

    def test_hosts_rejects_duplicate_name(self, monkeypatch, restore_config):
        with pytest.raises(SystemExit):
            _reload_config(monkeypatch, DOCKER_HOSTS="prod=ssh://a, prod=ssh://b")

    def test_hosts_rejects_reserved_local_name(self, monkeypatch, restore_config):
        with pytest.raises(SystemExit):
            _reload_config(monkeypatch, DOCKER_HOSTS="local=ssh://weird")

    def test_hosts_rejects_bad_characters(self, monkeypatch, restore_config):
        with pytest.raises(SystemExit):
            _reload_config(monkeypatch, DOCKER_HOSTS="n a s=ssh://x")

    def test_hosts_rejects_empty_name(self, monkeypatch, restore_config):
        with pytest.raises(SystemExit):
            _reload_config(monkeypatch, DOCKER_HOSTS="=ssh://x")

    def test_hosts_rejects_non_ssh_scheme(self, monkeypatch, restore_config):
        with pytest.raises(SystemExit):
            _reload_config(monkeypatch, DOCKER_HOSTS="prod=tcp://h:2376")

    def test_hosts_valid_round_trip_invariants(self, monkeypatch, restore_config):
        cfg = _reload_config(
            monkeypatch,
            DOCKER_HOSTS="prod=ssh://a,staging=ssh://b,edge=ssh://c",
        )
        names = [name for name, _ in cfg.DOCKER_HOSTS]
        urls = [url for _, url in cfg.DOCKER_HOSTS]
        assert urls[0] is None and names[0] == "local"
        assert len(names) == len(set(names))
        assert "local" not in names[1:]
        assert all(url.startswith("ssh://") for url in urls[1:])


class TestHostReachCooldown:
    def test_host_reach_cooldown_default_1h(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, HOST_REACH_COOLDOWN=None)
        assert cfg.HOST_REACH_COOLDOWN == timedelta(hours=1)

    def test_host_reach_cooldown_invalid_falls_back(self, monkeypatch, caplog, restore_config):
        with caplog.at_level(logging.WARNING, logger="dum"):
            cfg = _reload_config(monkeypatch, HOST_REACH_COOLDOWN="bogus")
        assert cfg.HOST_REACH_COOLDOWN == timedelta(hours=1)
        assert any(
            "HOST_REACH_COOLDOWN" in record.getMessage() and "bogus" in record.getMessage()
            for record in caplog.records
        )

    def test_host_reach_cooldown_custom(self, monkeypatch, restore_config):
        cfg = _reload_config(monkeypatch, HOST_REACH_COOLDOWN="4h")
        assert cfg.HOST_REACH_COOLDOWN == timedelta(hours=4)
