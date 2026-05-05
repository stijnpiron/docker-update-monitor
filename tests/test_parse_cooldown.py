"""Unit tests for parse_cooldown() — format parsing and edge cases."""

from datetime import timedelta

import pytest

from app.cooldown import parse_cooldown


class TestParseCooldownZero:
    def test_zero_string(self):
        assert parse_cooldown("0") == timedelta(0)

    def test_empty_string(self):
        assert parse_cooldown("") == timedelta(0)

    def test_whitespace_only(self):
        assert parse_cooldown("   ") == timedelta(0)


class TestParseCooldownHours:
    def test_one_hour(self):
        assert parse_cooldown("1h") == timedelta(hours=1)

    def test_twelve_hours(self):
        assert parse_cooldown("12h") == timedelta(hours=12)

    def test_large_hours(self):
        assert parse_cooldown("48h") == timedelta(hours=48)


class TestParseCooldownDays:
    def test_one_day(self):
        assert parse_cooldown("1d") == timedelta(days=1)

    def test_three_days(self):
        assert parse_cooldown("3d") == timedelta(days=3)

    def test_seven_days(self):
        assert parse_cooldown("7d") == timedelta(days=7)


class TestParseCooldownWeeks:
    def test_one_week(self):
        assert parse_cooldown("1w") == timedelta(weeks=1)

    def test_two_weeks(self):
        assert parse_cooldown("2w") == timedelta(weeks=2)


class TestParseCooldownMonths:
    def test_one_month(self):
        assert parse_cooldown("1m") == timedelta(days=30)

    def test_three_months(self):
        assert parse_cooldown("3m") == timedelta(days=90)


class TestParseCooldownWhitespace:
    def test_leading_and_trailing_whitespace(self):
        assert parse_cooldown("  12h  ") == timedelta(hours=12)


class TestParseCooldownInvalid:
    def test_unknown_unit_raises(self):
        with pytest.raises(ValueError, match="Invalid cooldown format"):
            parse_cooldown("5x")

    def test_missing_unit_raises(self):
        with pytest.raises(ValueError, match="Invalid cooldown format"):
            parse_cooldown("42")

    def test_text_only_raises(self):
        with pytest.raises(ValueError, match="Invalid cooldown format"):
            parse_cooldown("daily")

    def test_float_raises(self):
        with pytest.raises(ValueError, match="Invalid cooldown format"):
            parse_cooldown("1.5h")

    def test_negative_raises(self):
        with pytest.raises(ValueError, match="Invalid cooldown format"):
            parse_cooldown("-3d")
