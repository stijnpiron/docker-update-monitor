import re
from typing import Optional

from app.config import log


def parse_tag(tag: str, pattern: str) -> Optional[tuple[int, ...]]:
    """
    Match `tag` against `pattern` (fullmatch) and return all capture groups as ints.
    Returns None if the tag doesn't match or groups can't be cast to int.
    """
    m = re.fullmatch(pattern, tag)
    if not m:
        return None
    if not m.groups():
        raise ValueError(f"Pattern '{pattern}' matched '{tag}' but has no capture groups — wrap each version number in ()")
    try:
        return tuple(int(g) for g in m.groups())
    except (ValueError, TypeError):
        return None


def find_updates(
    current_tag: str,
    all_tags: list[str],
    pattern: str,
) -> dict[str, str]:
    """
    Given the current tag and a list of all available tags (filtered by `pattern`),
    return up to three "best" tags — one per update level.

    Adaptive comparison based on the number of capture groups:
      2 groups (major, minor):
        minor — same major, higher minor  → report only the highest minor
        major — higher major              → report only the highest major
      3+ groups (major, minor, patch, …):
        patch — same major + minor, higher patch/rest  → report only the highest
        minor — same major, higher minor               → report only the highest
        major — higher major                           → report only the highest

    Only the *best* (highest) candidate per level is returned, so you'll
    never get spammed with every intermediate version.
    """
    try:
        current = parse_tag(current_tag, pattern)
    except ValueError as exc:
        log.warning(f"    {exc}")
        return {}
    if current is None:
        log.warning(f"    Pattern '{pattern}' did not match current tag '{current_tag}'")
        return {}
    if len(current) < 2:
        log.warning(f"    Pattern '{pattern}' needs at least 2 capture groups (major, minor), got {len(current)}")
        return {}

    num_groups = len(current)

    # (version_tuple, tag_string) per level
    best: dict[str, tuple[tuple, str]] = {}

    for tag in all_tags:
        v = parse_tag(tag, pattern)
        if v is None or len(v) < num_groups:
            continue

        if num_groups == 2:
            maj, minor = v[0], v[1]
            cur_maj, cur_min = current[0], current[1]

            if maj == cur_maj and minor > cur_min:
                level = "minor"
            elif maj > cur_maj:
                level = "major"
            else:
                continue
        else:
            # 3+ groups: major, minor, patch (+ optional extras)
            maj, minor, rest = v[0], v[1], v[2:]
            cur_maj, cur_min, cur_rest = current[0], current[1], current[2:]

            if maj == cur_maj and minor == cur_min and rest > cur_rest:
                level = "patch"
            elif maj == cur_maj and minor > cur_min:
                level = "minor"
            elif maj > cur_maj:
                level = "major"
            else:
                continue

        current_best = best.get(level)
        if current_best is None or v > current_best[0]:
            best[level] = (v, tag)

    return {level: tag for level, (_, tag) in best.items()}
