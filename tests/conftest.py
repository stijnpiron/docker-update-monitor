"""Shared fixtures for the update-monitor test suite."""

import os
import tempfile
from unittest.mock import MagicMock, patch

import pytest

from app import http as http_mod
from app import main as main_mod


@pytest.fixture(autouse=True)
def _state_db_in_tmp(tmp_path):
    """Redirect the state DB to a temp directory for every test."""
    db_path = str(tmp_path / "state.db")
    with patch("app.config.STATE_DB_PATH", db_path):
        yield


@pytest.fixture
def mock_docker_client():
    """Return a mocked Docker client with containers.list() returning an empty list."""
    client = MagicMock()
    client.containers.list.return_value = []
    return client


@pytest.fixture
def mock_container():
    """Factory fixture to create mock containers with given attributes."""

    def _make(name="test-container", image_tag="nginx:1.0.0", labels=None):
        c = MagicMock()
        c.name = name
        c.labels = labels or {}
        c.image.tags = [image_tag]
        c.attrs = {"Config": {"Image": image_tag}}
        return c

    return _make


@pytest.fixture
def mock_http_session():
    """Patch app.http.http_session and return the mock."""
    with patch.object(http_mod, "http_session") as mock_session:
        yield mock_session


@pytest.fixture(autouse=True)
def reset_shutdown_flag():
    """Ensure shutdown_requested is reset between tests."""
    main_mod.shutdown_requested = False
    yield
    main_mod.shutdown_requested = False
