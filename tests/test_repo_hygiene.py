"""Repo hygiene: no operator home identity in the tracked tree (issue #21).

Shape-based, not secret-based. A guard that grepped for the literal username would
itself contain the username and fail its own assertion. Instead this asserts that
every Windows home path and every Claude Code project-dir slug in the tracked tree
names the repo's one sanctioned placeholder home, ``x`` -- the token already used
by src/citation_needed/discover.py:199, tests/test_discover.py:522 and
tests/test_review.py:812. Consequence: this also catches a FUTURE operator's
username, not just the one scrubbed in issue #21.

Enumeration is ``git ls-files``, deliberately NOT a ``db.PROJECT_ROOT`` filesystem
walk (the pattern used by tests/test_distill.py:1039 and tests/test_review.py:1290).
A walk would reach the gitignored data/citation.db, which legitimately stores real
project-dir locators re-derived by every ``cite scan``, and the guard would then
fail forever on content that was never in git.

Issue #21's stated done-when ("no case-insensitive match for c:\\users\\abero") is
NOT the criterion used here. Measured against the pre-scrub tree it reached 13 of
107 occurrences in 3 of 15 files, because it is blind to the forward-slash form (60
occurrences) and the project-dir slug form (34), which together were 88% of the
leak. This guard replaces that criterion.

DECLARED GAPS -- this is not total coverage. Not detected: percent-encoded forms,
JSON/Python double-escaped forms, drive-letter-less forms, POSIX /home/ forms,
WSL /mnt/c/ forms, and any hash or base64 derivative. All are currently absent from
the tree; the guard encodes today's shapes. It also cannot see git history or
GitHub issue bodies -- both remain open, see issue #21.
"""

from __future__ import annotations

import re
import subprocess

import pytest

from citation_needed import db

#: The one sanctioned placeholder home (src/citation_needed/discover.py:199).
ALLOWED_HOME_TOKENS = frozenset({"x"})

#: Windows home paths: any drive letter, either separator, any case of "users".
PATH_RE = re.compile(r"[A-Za-z]:[\\/]+(?i:users)[\\/]+(?!x(?:[\\/]|$))([A-Za-z0-9._$-]+)")

#: Claude Code project-dir slugs: the drive-dash-dash-Users-dash form.
SLUG_RE = re.compile(r"[A-Za-z]--(?i:users)-(?!x-)([A-Za-z0-9._$]+)")


def _tracked_files() -> list[str]:
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z"],
            cwd=db.PROJECT_ROOT,
            capture_output=True,
            check=False,
        )
    except OSError:  # pragma: no cover - git absent
        pytest.skip("git is unavailable; cannot enumerate the tracked tree")
    if proc.returncode != 0:  # pragma: no cover - not a work tree
        pytest.skip("git ls-files failed; cannot enumerate the tracked tree")
    return [p for p in proc.stdout.decode("utf-8").split("\0") if p]


def test_no_operator_home_identity_in_tracked_tree() -> None:
    """Every home path and project slug in the tracked tree uses the placeholder."""
    violations: list[str] = []
    for rel in _tracked_files():
        try:
            raw = (db.PROJECT_ROOT / rel).read_bytes()
        except OSError:
            continue
        if b"\0" in raw:  # binary
            continue
        text = raw.decode("utf-8", errors="ignore")
        for lineno, line in enumerate(text.splitlines(), start=1):
            for match in (*PATH_RE.finditer(line), *SLUG_RE.finditer(line)):
                if match.group(1).lower() not in ALLOWED_HOME_TOKENS:
                    violations.append(f"{rel}:{lineno}: {match.group(0)!r}")
    assert not violations, (
        "operator home identity in the tracked tree; replace the home segment with "
        "the placeholder 'x' (see src/citation_needed/discover.py:199):\n" + "\n".join(violations)
    )


def test_guard_detects_a_planted_violation() -> None:
    """Red-on-garbage anchor: the patterns must actually fire.

    Without this, a typo that made both regexes match nothing would leave
    ``test_no_operator_home_identity_in_tracked_tree`` permanently, silently green.
    """
    planted = [
        r"C:\Users\someone\dev",
        "c:/Users/someone/dev",
        "C:/USERS/someone",
        "c--Users-someone-dev",
        "C--Users-someone-dev-Alpha4Gate",
        "memory--c--users-someone-dev--note",
    ]
    for line in planted:
        assert PATH_RE.search(line) or SLUG_RE.search(line), line

    allowed = [
        r"C:\Users\x\dev",
        "c:/Users/x/dev",
        "c--Users-x-dev",
        "C--Users-x-dev-on-brand",
        "memory--c--users-x-dev--note",
        "c--somewhere-else-entirely",
        "file:///etc/passwd",
        "x-marks-the-spot",
    ]
    for line in allowed:
        for match in (*PATH_RE.finditer(line), *SLUG_RE.finditer(line)):
            assert match.group(1).lower() in ALLOWED_HOME_TOKENS, (line, match.group(0))
