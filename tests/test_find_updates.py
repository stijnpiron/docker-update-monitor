"""Unit tests for find_updates() — 2-group, 3-group, 4+ group, and edge cases."""

import pytest
from app.version import find_updates


class TestTwoGroupVersions:
    """2 capture groups: (major, minor)."""

    pattern = r"^(\d+)\.(\d+)$"

    def test_minor_update_detected(self):
        result = find_updates("18.15", ["18.20", "18.10", "17.0"], self.pattern)
        assert result["minor"] == "18.20"
        assert "major" not in result

    def test_major_update_detected(self):
        result = find_updates("18.15", ["20.0", "17.5"], self.pattern)
        assert result["major"] == "20.0"
        assert "minor" not in result

    def test_both_minor_and_major(self):
        result = find_updates("18.15", ["18.20", "20.0", "18.10"], self.pattern)
        assert result["minor"] == "18.20"
        assert result["major"] == "20.0"

    def test_no_updates(self):
        result = find_updates("18.15", ["18.15", "18.10", "17.0"], self.pattern)
        assert result == {}

    def test_picks_highest_minor(self):
        result = find_updates("18.15", ["18.16", "18.20", "18.18"], self.pattern)
        assert result["minor"] == "18.20"

    def test_picks_highest_major(self):
        result = find_updates("18.15", ["19.0", "20.5", "21.0"], self.pattern)
        assert result["major"] == "21.0"

    def test_no_patch_level(self):
        """2-group patterns should never produce a 'patch' update."""
        result = find_updates("18.15", ["18.20", "20.0"], self.pattern)
        assert "patch" not in result


class TestThreeGroupVersions:
    """3 capture groups: (major, minor, patch) — existing semver behavior."""

    pattern = r"^(\d+)\.(\d+)\.(\d+)$"

    def test_patch_update(self):
        result = find_updates("1.2.3", ["1.2.4", "1.2.5"], self.pattern)
        assert result["patch"] == "1.2.5"

    def test_minor_update(self):
        result = find_updates("1.2.3", ["1.3.0", "1.4.1"], self.pattern)
        assert result["minor"] == "1.4.1"

    def test_major_update(self):
        result = find_updates("1.2.3", ["2.0.0", "3.1.0"], self.pattern)
        assert result["major"] == "3.1.0"

    def test_all_levels(self):
        result = find_updates("1.2.3", ["1.2.5", "1.3.0", "2.0.0"], self.pattern)
        assert result["patch"] == "1.2.5"
        assert result["minor"] == "1.3.0"
        assert result["major"] == "2.0.0"

    def test_no_updates(self):
        result = find_updates("1.2.3", ["1.2.3", "1.2.2", "0.9.9"], self.pattern)
        assert result == {}

    def test_non_matching_tags_ignored(self):
        result = find_updates("1.2.3", ["latest", "alpine", "1.2.4"], self.pattern)
        assert result == {"patch": "1.2.4"}


class TestFourPlusGroupVersions:
    """4+ capture groups — extra groups compared lexicographically."""

    pattern = r"^(\d+)\.(\d+)\.(\d+)\.(\d+)$"

    def test_patch_with_fourth_group(self):
        result = find_updates("1.2.3.0", ["1.2.3.1", "1.2.3.5"], self.pattern)
        assert result["patch"] == "1.2.3.5"

    def test_minor_with_fourth_group(self):
        result = find_updates("1.2.3.0", ["1.3.0.0"], self.pattern)
        assert result["minor"] == "1.3.0.0"

    def test_major_with_fourth_group(self):
        result = find_updates("1.2.3.0", ["2.0.0.0"], self.pattern)
        assert result["major"] == "2.0.0.0"

    def test_lexicographic_comparison_third_group_wins(self):
        """(3,5) < (4,0) — third group takes precedence over fourth."""
        result = find_updates("1.2.3.5", ["1.2.4.0"], self.pattern)
        assert result["patch"] == "1.2.4.0"

    def test_lexicographic_comparison_picks_highest(self):
        """Among multiple patch candidates, pick the lexicographically highest tuple."""
        result = find_updates("1.2.3.0", ["1.2.3.9", "1.2.4.1", "1.2.4.0"], self.pattern)
        assert result["patch"] == "1.2.4.1"

    def test_fourth_group_alone_triggers_patch(self):
        """Only the 4th group differs — still a patch update."""
        result = find_updates("1.2.3.0", ["1.2.3.1"], self.pattern)
        assert result["patch"] == "1.2.3.1"


class TestEdgeCases:
    """Edge cases and error handling."""

    def test_single_group_warns_and_returns_empty(self):
        """Pattern with only 1 capture group should be skipped."""
        result = find_updates("5", ["6", "7"], r"^(\d+)$")
        assert result == {}

    def test_pattern_not_matching_current_tag(self):
        result = find_updates("latest", ["1.0.0"], r"^(\d+)\.(\d+)\.(\d+)$")
        assert result == {}

    def test_empty_tag_list(self):
        result = find_updates("1.2.3", [], r"^(\d+)\.(\d+)\.(\d+)$")
        assert result == {}

    def test_pattern_with_prefix(self):
        """Pattern that includes a 'v' prefix."""
        pattern = r"^v(\d+)\.(\d+)$"
        result = find_updates("v3.2", ["v3.5", "v4.0", "v2.9"], pattern)
        assert result["minor"] == "v3.5"
        assert result["major"] == "v4.0"
