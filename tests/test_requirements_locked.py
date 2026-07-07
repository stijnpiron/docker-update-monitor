"""Regression guard for issue #147: requirements.txt must stay fully locked.

The repo uses pip-compile to generate `requirements.txt` and
`requirements-dev.txt` from `requirements.in` / `requirements-dev.in`. The
generated files must:

  * pin every package with `==` (no `>=` floors)
  * include `--hash` entries for every package (so pip enables hash-checking
    automatically and Docker rebuilds are reproducible)

If any of these slips, Docker images can drift between rebuilds — which is
exactly the supply-chain hazard this project monitors *for*.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
LOCK_FILES = ("requirements.txt", "requirements-dev.txt")
SOURCE_FILES = ("requirements.in", "requirements-dev.in")

# Matches a package spec line at the start of a logical pin block, e.g.
#   flask==3.1.3 \
#   prometheus_client==0.25.0
PIN_LINE = re.compile(r"^[A-Za-z0-9_.\-]+==[^\s\\]+\s*\\?\s*$")


def _logical_lines(path: Path) -> list[str]:
    """Return non-comment, non-blank, non-`-r`-include lines."""
    return [
        line.rstrip()
        for line in path.read_text().splitlines()
        if line.strip() and not line.lstrip().startswith(("#", "-r ", "-c "))
    ]


@pytest.mark.parametrize("filename", LOCK_FILES)
def test_lock_file_exists(filename: str) -> None:
    assert (REPO_ROOT / filename).is_file(), f"{filename} missing"


@pytest.mark.parametrize("filename", SOURCE_FILES)
def test_source_in_file_exists(filename: str) -> None:
    assert (REPO_ROOT / filename).is_file(), f"{filename} missing"


@pytest.mark.parametrize("filename", LOCK_FILES)
def test_no_unpinned_floors_in_lock(filename: str) -> None:
    """No `>=`, `~=`, `>`, `<`, etc. — every package must be `==`-pinned."""
    bad: list[str] = []
    for line in _logical_lines(REPO_ROOT / filename):
        stripped = line.strip()
        # skip lines that are continuation/hash content
        if stripped.startswith("--hash="):
            continue
        # package spec lines either are exact pins or continuation lines for
        # hashes — anything else with a version operator is a floor.
        if any(op in stripped for op in (">=", "<=", "~=", "!=")) or re.search(
            r"(?<![=!<>~])[<>](?!=)", stripped
        ):
            bad.append(stripped)
    assert not bad, (
        f"{filename} contains unpinned version specifiers: {bad}. "
        "Re-run `pip-compile --generate-hashes` to regenerate."
    )


@pytest.mark.parametrize("filename", LOCK_FILES)
def test_every_package_has_hashes(filename: str) -> None:
    """Every `name==version` line should be followed by at least one --hash."""
    text = (REPO_ROOT / filename).read_text()
    pkg_pattern = re.compile(
        r"^([A-Za-z0-9_.\-]+)==[^\s\\]+\s*\\\s*\n((?:\s+--hash=sha256:[0-9a-f]+\s*\\?\s*\n)+)",
        re.MULTILINE,
    )
    matches = pkg_pattern.findall(text)
    assert matches, f"{filename} has no hashed pins"

    pin_starts = re.findall(
        r"^([A-Za-z0-9_.\-]+)==", text, flags=re.MULTILINE
    )
    hashed = {m[0] for m in matches}
    missing = [name for name in pin_starts if name not in hashed]
    assert not missing, (
        f"{filename} packages without hashes: {missing}. "
        "Re-run `pip-compile --generate-hashes`."
    )
