"""Unit tests for detect_registry() — table-driven tests for all registry types."""

import pytest

from app.registry.base import detect_registry


class TestDetectRegistryDockerHub:
    """Images that should resolve to 'dockerhub'."""

    def test_simple_official_image(self):
        assert detect_registry("nginx") == "dockerhub"

    def test_namespaced_image(self):
        assert detect_registry("linuxserver/sonarr") == "dockerhub"

    def test_explicit_docker_io_prefix(self):
        assert detect_registry("docker.io/library/nginx") == "dockerhub"

    def test_docker_io_namespaced(self):
        assert detect_registry("docker.io/bitnami/redis") == "dockerhub"

    def test_two_part_no_dots(self):
        assert detect_registry("myuser/myapp") == "dockerhub"


class TestDetectRegistryGHCR:
    """Images that should resolve to 'ghcr'."""

    def test_ghcr_standard(self):
        assert detect_registry("ghcr.io/owner/repo") == "ghcr"

    def test_ghcr_nested_path(self):
        assert detect_registry("ghcr.io/org/group/image") == "ghcr"

    def test_ghcr_with_tag_in_name(self):
        # detect_registry operates on image_name (tag already stripped)
        assert detect_registry("ghcr.io/user/app") == "ghcr"


class TestDetectRegistryLSCR:
    """Images from lscr.io that should resolve to 'ghcr' (GHCR alias)."""

    def test_lscr_standard(self):
        assert detect_registry("lscr.io/linuxserver/bazarr") == "ghcr"

    def test_lscr_with_tag_in_first_segment(self):
        assert detect_registry("lscr.io/linuxserver/sonarr") == "ghcr"

    def test_lscr_nested_path(self):
        assert detect_registry("lscr.io/linuxserver/bazarr") == "ghcr"


class TestDetectRegistryUnknown:
    """Images from unsupported registries."""

    def test_custom_registry_with_port(self):
        assert detect_registry("registry.example.com:5000/ns/image") == "unknown"

    def test_quay_io(self):
        assert detect_registry("quay.io/prometheus/node-exporter") == "unknown"

    def test_gcr_io(self):
        assert detect_registry("gcr.io/google-containers/pause") == "unknown"

    def test_custom_domain(self):
        assert detect_registry("my.registry.local/app") == "unknown"


class TestDetectRegistryURL:
    """Images specified as full URLs (with scheme)."""

    def test_ghcr_url(self):
        assert detect_registry("https://ghcr.io/owner/repo") == "ghcr"

    def test_lscr_url(self):
        assert detect_registry("https://lscr.io/linuxserver/sonarr") == "ghcr"

    def test_dockerhub_url(self):
        assert detect_registry("https://docker.io/library/nginx") == "dockerhub"

    def test_unknown_url(self):
        assert detect_registry("https://quay.io/prometheus/node-exporter") == "unknown"
