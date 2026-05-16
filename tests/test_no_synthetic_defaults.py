"""Scan source for placeholder copy + numeric defaults that previously lied.

The previous build hardcoded `STARTING_EQUITY_DEFAULT = 10_000` and templated
`|| 10000` fallbacks that masked the real broker numbers. This test fails
the build if those patterns reappear.

Allowlist: intentional constants are listed at the top. Add new ones here
ONLY with a comment explaining why they cannot lie (e.g. "max element height
in pixels — has nothing to do with money").
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

SCAN_PATHS = [
    ROOT / "tools" / "publisher.py",
    ROOT / "src"   / "template.html",
    ROOT / "scripts" / "build.py",
    ROOT / "scripts" / "verify_against_truth.py",
]

# Forbidden literal substrings — these are known prior-art placeholder copy.
FORBIDDEN_SUBSTRINGS = [
    "$10K",
    "$0 in losers",
    "Skeptic",
    "skeptic",
    "brag",
    "Brag",
    "cage match",
    "persona",
    "ai_learnings",
    "translations",
    "DAY0",
    "demo mode",
    "Tom's Highlights",
    "STARTING_EQUITY_DEFAULT",
]

# Forbidden numeric-default patterns inside JS / Python that fall back when
# real data is missing — these are exactly the bugs the rebuild exists to
# eliminate.
FORBIDDEN_REGEXES = [
    re.compile(r"\|\|\s*10000\b"),     # JS fallback: ... || 10000
    re.compile(r"\|\|\s*5000\b"),      # JS fallback: ... || 5000
    re.compile(r"\|\|\s*\$?5,?000\b"),
    re.compile(r"=\s*10000\.?0?\b"),   # Python: starting = 10000
    re.compile(r"=\s*5000\.?0?\b"),    # Python: equity = 5000
    re.compile(r"or\s+10000\b"),       # Python: x or 10000
    re.compile(r"or\s+5000\b"),
]

# Allowlisted occurrences (file path → list of substrings that may appear).
# Keep this list short; add only with a justifying comment.
ALLOWLIST: dict[str, list[str]] = {
    # No allowlist needed at v3.0. If a future change needs one, add it
    # here and explain why the literal cannot be a placeholder for money.
}


def _is_allowlisted(path: Path, line: str) -> bool:
    rel = str(path.relative_to(ROOT))
    for allowed in ALLOWLIST.get(rel, []):
        if allowed in line:
            return True
    return False


def test_no_forbidden_substrings():
    hits = []
    for path in SCAN_PATHS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for needle in FORBIDDEN_SUBSTRINGS:
            for i, line in enumerate(text.splitlines(), start=1):
                if needle in line and not _is_allowlisted(path, line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}: forbidden substring {needle!r}")
    assert not hits, "Forbidden placeholder copy found:\n" + "\n".join(hits)


def test_no_forbidden_numeric_defaults():
    hits = []
    for path in SCAN_PATHS:
        if not path.exists():
            continue
        text = path.read_text(encoding="utf-8")
        for i, line in enumerate(text.splitlines(), start=1):
            for rx in FORBIDDEN_REGEXES:
                if rx.search(line) and not _is_allowlisted(path, line):
                    hits.append(f"{path.relative_to(ROOT)}:{i}: forbidden default ({rx.pattern}) in: {line.strip()}")
    assert not hits, "Synthetic numeric defaults found:\n" + "\n".join(hits)
