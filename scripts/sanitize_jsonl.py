#!/usr/bin/env python3
"""Strip PII fields from a quant-bot live JSONL before publishing.

Reads stdin (one JSON object per line) and writes scrubbed JSONL to stdout.
Drops a fixed list of sensitive fields from every event:

  account_id    — IBKR account number (e.g. "U25438416", "DUP187165")
  client_id     — IBKR API client identifier
  host          — broker host
  port          — broker port
  bot_pid       — local process ID

  all_positions       — full position book (size + symbol detail)
  open_position       — currently open position detail
  expected_open_position — pre-trade expected position

  equity_usd          — absolute account equity (reveals account size)
  starting_equity_usd — opening equity for the session
  drawdown_pct        — drawdown relative to peak (reveals account size)

Everything else passes through unchanged. Per-trade P&L (in dollars) and
trade events (bracket_submitted/filled/closed, qty, side, entry, stop,
target, sub_signal, regime) are NOT stripped — those are the data the
dashboard needs to render.

Usage:
  cat input.jsonl | python3 sanitize_jsonl.py > output.jsonl
  python3 sanitize_jsonl.py < input.jsonl > output.jsonl

The script never raises on a malformed line — it logs to stderr and
continues, so a single bad line cannot break a sync.
"""
from __future__ import annotations

import json
import sys

# Single source of truth — keep this list aligned with the README + the
# privacy section of the public repo. Add a field name here BEFORE the
# bot starts emitting it if it could be PII.
PII_FIELDS = frozenset({
    # Connection / identity — broker account, machine, process.
    "account_id",
    "client_id",
    "host",
    "port",
    "bot_pid",
    # Positions — actual size + symbol exposure on the book.
    "all_positions",
    "open_position",
    "expected_open_position",
    # Account size / drawdown / equity — anything that reveals real-money
    # principal or running balance. Per-trade pnl_dollars (the small,
    # bounded number the dashboard shows in "today wins/losses") is NOT
    # in this list — it's the whole point of the dashboard.
    "equity_usd",
    "starting_equity_usd",
    "current_equity_usd",
    "equity_after_usd",
    "drawdown_pct",
    # Risk-budget knobs — reveal the bot's per-trade dollar limits which
    # combined with public position sizing rules would back into account
    # size. Strip them.
    "actual_pct_risk",
    "max_pct_risk_per_trade",
    "per_contract_risk_usd",
    "effective_risk_usd",
    "skipped_risk",
    # Cost-budget knobs — same reasoning. Per-trade fees are an internal
    # operational concern; absolute dollar caps reveal account size.
    "cost_alert_usd",
    "cost_halt_usd",
    "cost_pause_usd",
    "cost_usd",
    "rolling_cost_usd",
    "net_pnl_usd",
    "pnl_usd",
})


def scrub(obj):
    """Recursively remove PII keys from a JSON value.

    Walks dicts and lists at every depth, dropping any key whose name is in
    PII_FIELDS. Empirically the bot nests sensitive numbers inside blocks
    like `snapshot` (e.g. ``snapshot.equity_usd: 1100.0``), so a top-level
    pass alone leaks. Recursion catches them all — and is a no-op on
    primitives, so it's safe to apply uniformly.
    """
    if isinstance(obj, dict):
        return {k: scrub(v) for k, v in obj.items() if k not in PII_FIELDS}
    if isinstance(obj, list):
        return [scrub(x) for x in obj]
    return obj


def main() -> int:
    n_in = 0
    n_out = 0
    n_bad = 0
    for line in sys.stdin:
        line = line.rstrip("\n")
        if not line:
            continue
        n_in += 1
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            print(f"sanitize: skipping malformed line {n_in}", file=sys.stderr)
            n_bad += 1
            continue
        if not isinstance(obj, dict):
            n_bad += 1
            continue
        sys.stdout.write(json.dumps(scrub(obj), separators=(",", ":")) + "\n")
        n_out += 1
    print(
        f"sanitize: in={n_in} out={n_out} bad={n_bad} stripped_fields={sorted(PII_FIELDS)}",
        file=sys.stderr,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
