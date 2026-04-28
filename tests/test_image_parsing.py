"""Unit tests for image reference parsing (digest handling)."""

import pytest


def parse_image_ref(image_ref: str) -> tuple[str, str]:
    """Replicate the parsing logic from monitor.py."""
    # Strip digest suffix if present
    if "@" in image_ref:
        image_ref = image_ref.split("@")[0]

    if ":" in image_ref.split("/")[-1]:
        image_name, current_tag = image_ref.rsplit(":", 1)
    else:
        image_name, current_tag = image_ref, "latest"

    return image_name, current_tag


class TestParseImageRef:
    """Tests for image reference parsing with digest handling."""

    def test_simple_image_with_tag(self):
        assert parse_image_ref("nginx:1.25") == ("nginx", "1.25")

    def test_simple_image_no_tag(self):
        assert parse_image_ref("nginx") == ("nginx", "latest")

    def test_image_with_registry_port_and_tag(self):
        assert parse_image_ref("registry.example.com:5000/ns/image:2.0") == (
            "registry.example.com:5000/ns/image",
            "2.0",
        )

    def test_image_with_registry_port_no_tag(self):
        assert parse_image_ref("registry.example.com:5000/ns/image") == (
            "registry.example.com:5000/ns/image",
            "latest",
        )

    def test_digest_only(self):
        """image@sha256:abcdef → image_name='image', current_tag='latest'"""
        assert parse_image_ref("image@sha256:abcdef") == ("image", "latest")

    def test_tag_plus_digest(self):
        """image:1.0.0@sha256:abcdef → image_name='image', current_tag='1.0.0'"""
        assert parse_image_ref("image:1.0.0@sha256:abcdef") == ("image", "1.0.0")

    def test_registry_port_tag_and_digest(self):
        """registry.example.com:5000/ns/image:2.0@sha256:abc → correct split."""
        assert parse_image_ref("registry.example.com:5000/ns/image:2.0@sha256:abc") == (
            "registry.example.com:5000/ns/image",
            "2.0",
        )

    def test_registry_port_digest_only(self):
        """registry.example.com:5000/ns/image@sha256:abc → latest."""
        assert parse_image_ref("registry.example.com:5000/ns/image@sha256:abc") == (
            "registry.example.com:5000/ns/image",
            "latest",
        )

    def test_namespace_image_with_tag(self):
        assert parse_image_ref("library/nginx:alpine") == ("library/nginx", "alpine")

    def test_namespace_image_no_tag(self):
        assert parse_image_ref("library/nginx") == ("library/nginx", "latest")
