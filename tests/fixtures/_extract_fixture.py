"""Trim a quant-bot live JSONL into a publisher-test fixture.

Keeps only the events the publisher reads + only the fields the schema's
allow-list lets through. Output is several orders of magnitude smaller
than the raw journal and contains no PII.

This script is run once when fixtures are pinned; the output is
committed to tests/fixtures/real_journals/ and the original raw journals
are NOT committed.

Usage:
    python3 tests/fixtures/_extract_fixture.py SRC_DIR DST_DIR
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

# Events the publisher actually reads — matches LIVE_TRADE_EVENTS in
# publisher.py + the orchestrator_starting + bracket_submitted events
# used for trade reconstruction.
KEEP_EVENTS = {
    "orchestrator_starting",
    "bracket_submitted",
    "fill",
    "position_closed",
    "trade_closed",
    "bracket_filled",  # entry-side; kept for mode detection invariants
}

# PII fields that must NEVER appear in committed fixtures, even though
# the publisher's allow-list strips them downstream. Defense in depth.
PII_FIELDS = frozenset({
    "account_id", "client_id", "host", "port", "bot_pid",
    "all_positions", "open_position", "expected_open_position",
})


def scrub(o):
    """Recursively drop PII fields. Same allow-list discipline as the publisher."""
    if isinstance(o, dict):
        return {k: scrub(v) for k, v in o.items() if k not in PII_FIELDS}
    if isinstance(o, list):
        return [scrub(x) for x in o]
    return o


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: _extract_fixture.py SRC_DIR DST_DIR", file=sys.stderr)
        return 2
    src = Path(sys.argv[1])
    dst = Path(sys.argv[2])
    dst.mkdir(parents=True, exist_ok=True)
    for jsonl in sorted(src.glob("*.jsonl")):
        kept = 0
        out_path = dst / jsonl.name
        with jsonl.open() as fh, out_path.open("w") as out:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if ev.get("event") not in KEEP_EVENTS:
                    continue
                ev = scrub(ev)
                out.write(json.dumps(ev, separators=(",", ":")) + "\n")
                kept += 1
        print(f"{jsonl.name}: {kept} events kept ({out_path.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
