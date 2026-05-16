#!/usr/bin/env python3
"""Independent re-verification of data/dashboard.json against raw journal events.

Runs in CI and locally. Walks journal/live/*.jsonl WITHOUT importing the
publisher, recomputes the five invariant numbers, and diffs them against
data/dashboard.json. ANY divergence > $0.01 fails the build.

This is the second pair of eyes that catches publisher bugs.

Usage:
  python3 scripts/verify_against_truth.py --qb-root /path/to/quant-bot
  python3 scripts/verify_against_truth.py --qb-root /path/to/quant-bot --json data/dashboard.json
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys

EPSILON = 0.01


def load_events(journal_dir: str) -> list[dict]:
    out = []
    for path in sorted(glob.glob(os.path.join(journal_dir, "*.jsonl"))):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if e.get("_replay") is True:
                    continue
                bid = e.get("bracket_id") or ""
                if isinstance(bid, str) and bid.startswith("BR-test"):
                    continue
                out.append(e)
    return out


def compute_truth(events: list[dict]) -> dict:
    submitted: dict[str, dict] = {}
    closes: list[dict] = []
    for e in events:
        ev = e.get("event")
        bid = e.get("bracket_id")
        if not bid:
            continue
        if ev == "bracket_submitted":
            submitted[bid] = e
        elif ev == "position_closed":
            if bid in submitted:
                closes.append(e)

    closes.sort(key=lambda x: x["ts"])
    if not closes:
        raise SystemExit("verify: no closes in journal — cannot compute truth")

    first = closes[0]
    last = closes[-1]
    starting = float(first["equity_after_usd"]) - float(first["net_pnl_usd"])
    current = float(last["equity_after_usd"])
    realized = sum(float(c["net_pnl_usd"]) for c in closes)
    wins = sum(1 for c in closes if float(c["net_pnl_usd"]) > 0)
    losses = len(closes) - wins
    return {
        "starting_equity": round(starting, 2),
        "current_equity": round(current, 2),
        "account_change_usd": round(current - starting, 2),
        "realized_trade_pnl_usd": round(realized, 2),
        "cost_drift_usd": round(realized - (current - starting), 2),
        "total_trades": len(closes),
        "wins": wins,
        "losses": losses,
    }


def diff(truth: dict, dashboard: dict) -> list[str]:
    errs = []
    bot = dashboard.get("bot", {})
    head = dashboard.get("headline", {})

    pairs = [
        ("starting_equity",        truth["starting_equity"],        bot.get("starting_equity")),
        ("current_equity",         truth["current_equity"],         bot.get("current_equity")),
        ("account_change_usd",     truth["account_change_usd"],     head.get("account_change_usd")),
        ("realized_trade_pnl_usd", truth["realized_trade_pnl_usd"], head.get("realized_trade_pnl_usd")),
        ("cost_drift_usd",         truth["cost_drift_usd"],         head.get("cost_drift_usd")),
    ]
    for name, expected, actual in pairs:
        if actual is None:
            errs.append(f"{name}: missing in dashboard.json (expected {expected})")
        elif abs(float(actual) - float(expected)) > EPSILON:
            errs.append(f"{name}: dashboard={actual} expected={expected} diff={float(actual)-float(expected):+.4f}")

    int_pairs = [
        ("total_trades", truth["total_trades"], head.get("total_trades")),
        ("wins",         truth["wins"],         head.get("wins")),
        ("losses",       truth["losses"],       head.get("losses")),
    ]
    for name, expected, actual in int_pairs:
        if actual is None:
            errs.append(f"{name}: missing (expected {expected})")
        elif int(actual) != int(expected):
            errs.append(f"{name}: dashboard={actual} expected={expected}")
    return errs


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--qb-root", required=True, help="quant-bot repo root")
    p.add_argument("--json", default="data/dashboard.json", help="dashboard.json path")
    args = p.parse_args(argv)

    journal_dir = os.path.join(args.qb_root, "journal", "live")
    events = load_events(journal_dir)
    truth = compute_truth(events)

    with open(args.json, "r", encoding="utf-8") as fh:
        dashboard = json.load(fh)

    errs = diff(truth, dashboard)
    if errs:
        print("verify: FAIL", file=sys.stderr)
        for e in errs:
            print("  - " + e, file=sys.stderr)
        return 2

    print(
        "verify: OK · "
        f"starting=${truth['starting_equity']:.2f} "
        f"current=${truth['current_equity']:.2f} "
        f"change={truth['account_change_usd']:+.2f} "
        f"trades={truth['total_trades']} "
        f"drift={truth['cost_drift_usd']:+.2f}"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
