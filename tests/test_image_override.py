"""Tests for the `image-override` label.

A container whose image is built locally (no registry presence) can point its
check at the upstream image it derives from via
`<PREFIX>.image-override: <name>:<tag>`. Covers semver detection through the
override, invalid labels, and the digest-mode rejection.
"""

from unittest.mock import MagicMock, patch

from app import config as config_mod
from app import scanner as scanner_mod
from app import state as state_mod


_LOCAL_ONLY = [("local", None)]

_SEMVER = r"^(\d+)\.(\d+)\.(\d+)$"


def _make_container(name, image_tag, labels=None):
    c = MagicMock()
    c.name = name
    c.labels = labels or {}
    c.image.tags = [image_tag]
    c.image.attrs = {}
    c.attrs = {"Config": {"Image": image_tag}}
    return c


def _container_client(name, containers):
    c = MagicMock(name=f"client-{name}")
    c.containers.list.return_value = list(containers)
    return c


def _local(container):
    return [
        patch.object(scanner_mod, "notify"),
        patch.object(scanner_mod, "get_dockerhub_token", return_value="tok"),
        patch.object(scanner_mod, "docker"),
        patch.object(config_mod, "DOCKER_HOSTS", _LOCAL_ONLY),
        patch.object(config_mod, "GITHUB_TOKEN", ""),
    ]


def _run(container, fetch_tags=None):
    mocks = _local(container)
    with mocks[0] as mock_notify, mocks[1], mocks[2] as mock_docker, mocks[3], mocks[4]:
        mock_docker.from_env.return_value = _container_client("local", [container])
        if fetch_tags is not None:
            with patch.object(scanner_mod, "fetch_all_tags", return_value=fetch_tags):
                scanner_mod.run_check()
        else:
            scanner_mod.run_check()
    return mock_notify


class TestImageOverrideSemver:
    """Override redirects semver detection to the upstream image."""

    def test_update_found_via_override(self):
        """A locally built image tracking upstream 1.6.2 reports 1.6.3."""
        container = _make_container(
            "bazarr", "ghcr.io/stijnpiron/bazarr-whisper-stream:1.6.2-1",
            {
                f"{config_mod.LABEL_PREFIX}.tag-regex": _SEMVER,
                f"{config_mod.LABEL_PREFIX}.image-override":
                    "lscr.io/linuxserver/bazarr:1.6.2",
            },
        )
        mock_notify = _run(container, ["1.6.2", "1.6.3", "1.7.0"])

        actionable = mock_notify.call_args.args[0]
        assert len(actionable) == 2
        by_type = {u.update_type: u for u in actionable}
        assert by_type["patch"].image == "lscr.io/linuxserver/bazarr"
        assert by_type["patch"].current_version == "1.6.2"
        assert by_type["patch"].new_version == "1.6.3"
        assert by_type["minor"].current_version == "1.6.2"
        assert by_type["minor"].new_version == "1.7.0"

        rows = [r for r in state_mod.get_all_updates()
                if r["container_name"] == "bazarr"]
        assert len(rows) == 2
        assert all(r["image"] == "lscr.io/linuxserver/bazarr" for r in rows)

    def test_no_update_when_upstream_current(self):
        container = _make_container(
            "bazarr", "ghcr.io/stijnpiron/bazarr-whisper-stream:1.6.2-1",
            {
                f"{config_mod.LABEL_PREFIX}.tag-regex": _SEMVER,
                f"{config_mod.LABEL_PREFIX}.image-override":
                    "lscr.io/linuxserver/bazarr:1.6.2",
            },
        )
        mock_notify = _run(container, ["1.6.2"])

        actionable = mock_notify.call_args.args[0]
        assert actionable == []


class TestImageOverrideValidation:
    """Invalid override labels warn and skip instead of crashing."""

    def test_override_without_tag_rejected(self):
        container = _make_container(
            "bazarr", "ghcr.io/stijnpiron/bazarr-whisper-stream:1.6.2-1",
            {
                f"{config_mod.LABEL_PREFIX}.tag-regex": _SEMVER,
                f"{config_mod.LABEL_PREFIX}.image-override":
                    "lscr.io/linuxserver/bazarr",
            },
        )
        mock_notify = _run(container, ["1.6.2"])

        actionable = mock_notify.call_args.args[0]
        assert actionable == []
        warnings = mock_notify.call_args.kwargs["warnings"]
        assert len(warnings) == 1
        assert "must include a tag" in warnings[0].message

    def test_override_with_digest_mode_rejected(self):
        container = _make_container(
            "bazarr", "ghcr.io/stijnpiron/bazarr-whisper-stream:1.6.2-1",
            {
                f"{config_mod.LABEL_PREFIX}.image-override":
                    "lscr.io/linuxserver/bazarr:1.6.2",
                f"{config_mod.LABEL_PREFIX}.mode": "digest",
            },
        )
        mock_notify = _run(container)

        actionable = mock_notify.call_args.args[0]
        assert actionable == []
        warnings = mock_notify.call_args.kwargs["warnings"]
        assert len(warnings) == 1
        assert "only supports semver" in warnings[0].message
