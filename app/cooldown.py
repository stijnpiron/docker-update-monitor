"""Utilities for parsing and applying update cooldown periods."""

import re
from datetime import timedelta


def parse_cooldown(value: str) -> timedelta:
    """Parse a cooldown string into a :class:`timedelta`.

    Accepted formats:
    - ``"0"`` or ``""`` — no cooldown
    - ``"<n>h"`` — *n* hours
    - ``"<n>d"`` — *n* days
    - ``"<n>w"`` — *n* weeks
    - ``"<n>m"`` — *n* months (≈ 30 days each)

    Examples::

        parse_cooldown("0")   → timedelta(0)
        parse_cooldown("12h") → timedelta(hours=12)
        parse_cooldown("3d")  → timedelta(days=3)
        parse_cooldown("2w")  → timedelta(weeks=2)
        parse_cooldown("1m")  → timedelta(days=30)

    Raises :class:`ValueError` for unrecognised formats.
    """
    value = value.strip()
    if not value or value == "0":
        return timedelta(0)

    m = re.fullmatch(r"(\d+)([hdwm])", value)
    if not m:
        raise ValueError(
            f"Invalid cooldown format '{value}'. "
            "Expected '0', or a positive integer followed by h/d/w/m (e.g. '12h', '3d', '2w', '1m')."
        )

    amount = int(m.group(1))
    unit = m.group(2)

    if unit == "h":
        return timedelta(hours=amount)
    if unit == "d":
        return timedelta(days=amount)
    if unit == "w":
        return timedelta(weeks=amount)
    # unit == "m"
    return timedelta(days=amount * 30)
