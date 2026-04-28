"""Unit tests for parse_tag() — exhaustive coverage of valid, invalid, and edge cases."""

import pytest

from app.version import parse_tag


class TestParseTagValidSemver:
    """Standard semver pattern with 3 capture groups."""

    pattern = r"^v?(\d+)\.(\d+)\.(\d+)$"

    def test_basic_semver(self):
        assert parse_tag("1.2.3", self.pattern) == (1, 2, 3)

    def test_semver_with_v_prefix(self):
        assert parse_tag("v1.2.3", self.pattern) == (1, 2, 3)

    def test_high_version_numbers(self):
        assert parse_tag("100.200.300", self.pattern) == (100, 200, 300)

    def test_zero_version(self):
        assert parse_tag("0.0.0", self.pattern) == (0, 0, 0)

    def test_single_digit_each(self):
        assert parse_tag("9.8.7", self.pattern) == (9, 8, 7)


class TestParseTagTwoGroups:
    """Two capture groups (major, minor)."""

    pattern = r"^(\d+)\.(\d+)$"

    def test_two_group_basic(self):
        assert parse_tag("18.15", self.pattern) == (18, 15)

    def test_two_group_zero(self):
        assert parse_tag("0.1", self.pattern) == (0, 1)


class TestParseTagNoMatch:
    """Tags that don't match the pattern return None."""

    pattern = r"^(\d+)\.(\d+)\.(\d+)$"

    def test_non_numeric_tag(self):
        assert parse_tag("latest", self.pattern) is None

    def test_alpha_suffix(self):
        assert parse_tag("1.2.3-beta", self.pattern) is None

    def test_partial_match(self):
        assert parse_tag("1.2", self.pattern) is None

    def test_empty_string(self):
        assert parse_tag("", self.pattern) is None

    def test_extra_segments(self):
        assert parse_tag("1.2.3.4", self.pattern) is None


class TestParseTagNoGroups:
    """Pattern without capture groups raises ValueError."""

    def test_no_capture_groups_raises(self):
        with pytest.raises(ValueError, match="no capture groups"):
            parse_tag("hello", r"^hello$")


class TestParseTagNonIntegerGroups:
    """Groups that can't be cast to int return None."""

    def test_non_numeric_capture_group(self):
        # Pattern captures letters — int() will fail
        assert parse_tag("abc.def", r"^([a-z]+)\.([a-z]+)$") is None


class TestParseTagComplexPatterns:
    """Patterns with prefixes, suffixes, or optional parts."""

    def test_prefix_and_suffix(self):
        pattern = r"^release-(\d+)\.(\d+)\.(\d+)-stable$"
        assert parse_tag("release-2.5.1-stable", pattern) == (2, 5, 1)

    def test_optional_v_prefix(self):
        pattern = r"^v?(\d+)\.(\d+)$"
        assert parse_tag("v3.2", pattern) == (3, 2)
        assert parse_tag("3.2", pattern) == (3, 2)

    def test_four_groups(self):
        pattern = r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$"
        assert parse_tag("1.2.3.4", pattern) == (1, 2, 3, 4)
