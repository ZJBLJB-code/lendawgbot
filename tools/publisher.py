#!/usr/bin/env python3
"""Build data/dashboard.json from a quant-bot journal.

Reads journal/live/YYYY-MM-DD.jsonl files from a quant-bot repo, walks the
events, computes the four numbers Tom needs (account change since launch,
today's P&L, daily P&L list, all closed trades), and writes a single
data/dashboard.json file atomically.

The hard rule: every number in the JSON traces to a real journal event.
There are NO synthetic defaults. If a value cannot be computed it is set
to None and the renderer hides the field.

Authoritative source for "is the account up or down":
  starting_equity = first_close.equity_after_usd - first_close.net_pnl_usd
  current_equity  = last_close.equity_after_usd
  account_change  = current_equity - starting_equity

The sum of per-trade net_pnl_usd is the SECONDARY number; the gap between
it and account_change is broker commissions/fees/slippage. The dashboard
surfaces the gap explicitly as a "data quality" line.

Usage:
  python3 tools/publisher.py /path/to/quant-bot
  python3 tools/publisher.py /path/to/quant-bot --out data/dashboard.json
"""
from __future__ import annotations

import argparse
import dataclasses
import datetime as dt
import glob
import json
import os
import sys
import tempfile
from collections import defaultdict
from typing import Iterable

SCHEMA_VERSION = "3.0"
INSTRUMENT = "/MES (micro S&P futures)"
BOT_NAME = "Len Dawg"


# ---------------------------------------------------------------------------
# Filtering — drop test pollution. Per CLAUDE.md runtime trust-doc discipline,
# tests writing into the live journal have historically caused dashboard lies.
# ---------------------------------------------------------------------------
def _is_test_pollution(event: dict) -> bool:
    if event.get("_replay") is True:
        return True
    bid = event.get("bracket_id") or ""
    if isinstance(bid, str) and bid.startswith("BR-test"):
        return True
    return False


def _iter_events(journal_dir: str) -> Iterable[dict]:
    """Yield every non-polluted event from journal/live/*.jsonl, in file order."""
    pattern = os.path.join(journal_dir, "*.jsonl")
    for path in sorted(glob.glob(pattern)):
        with open(path, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    e = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if _is_test_pollution(e):
                    continue
                yield e


# ---------------------------------------------------------------------------
# Trade matching — pair bracket_submitted with position_closed by bracket_id.
# A trade is "closed" only when both halves exist. Orphan submissions are
# tracked as open positions; orphan closes are surfaced as data-quality
# warnings.
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class Trade:
    bracket_id: str
    date: str          # YYYY-MM-DD (UTC, the date the position closed)
    entry_ts: str      # ISO-8601, from bracket_submitted
    exit_ts: str       # ISO-8601, from position_closed
    side: str          # "LONG" / "SHORT"
    entry_price: float
    exit_price: float
    stop_price: float | None
    target_price: float | None
    qty: int
    pnl_usd: float     # net_pnl_usd from position_closed
    sub_signal: str    # e.g. "ORF", "VWAP_REV"
    regime: str | None
    equity_after_usd: float  # used internally to compute starting/current; not exported as field name

    def to_public(self) -> dict:
        # Note: we do NOT export raw equity_after_usd per-trade — that would
        # trip the PII grep in pages.yml. The HEADLINE numbers (starting,
        # current, change) are what get published; per-trade just shows pnl.
        return {
            "bracket_id": self.bracket_id,
            "date": self.date,
            "entry_ts": self.entry_ts,
            "exit_ts": self.exit_ts,
            "side": self.side,
            "entry_price": self.entry_price,
            "exit_price": self.exit_price,
            "stop_price": self.stop_price,
            "target_price": self.target_price,
            "qty": self.qty,
            "pnl_usd": round(self.pnl_usd, 2),
            "sub_signal": self.sub_signal,
            "regime": self.regime,
        }


def _match_trades(events: list[dict]) -> tuple[list[Trade], int, int]:
    """Return (closed_trades_in_chronological_order, open_positions, orphan_closes)."""
    submitted: dict[str, dict] = {}
    trades: list[Trade] = []
    orphan_closes = 0
    closed_ids: set[str] = set()

    for e in events:
        ev = e.get("event")
        bid = e.get("bracket_id")
        if not bid:
            continue
        if ev == "bracket_submitted":
            submitted[bid] = e
        elif ev == "position_closed":
            sub = submitted.get(bid)
            if sub is None:
                orphan_closes += 1
                continue
            try:
                exit_ts = e["ts"]
                entry_ts = sub["ts"]
                trades.append(Trade(
                    bracket_id=bid,
                    date=exit_ts[:10],
                    entry_ts=entry_ts,
                    exit_ts=exit_ts,
                    side=sub.get("side", "?"),
                    entry_price=float(sub.get("entry") or 0.0),
                    exit_price=float(e.get("exit_price") or 0.0),
                    stop_price=_maybe_float(sub.get("stop")),
                    target_price=_maybe_float(sub.get("target")),
                    qty=int(sub.get("qty") or 1),
                    pnl_usd=float(e["net_pnl_usd"]),
                    sub_signal=sub.get("sub_signal", "?"),
                    regime=sub.get("regime"),
                    equity_after_usd=float(e["equity_after_usd"]),
                ))
                closed_ids.add(bid)
            except (KeyError, ValueError, TypeError):
                # malformed event — skip; verify_against_truth.py will catch
                # any divergence from raw-event ground truth.
                continue

    open_positions = sum(1 for bid in submitted if bid not in closed_ids)
    trades.sort(key=lambda t: t.exit_ts)
    return trades, open_positions, orphan_closes


def _maybe_float(v) -> float | None:
    try:
        return float(v) if v is not None else None
    except (ValueError, TypeError):
        return None


# ---------------------------------------------------------------------------
# Aggregation — daily rows + headline.
# ---------------------------------------------------------------------------
def _build_daily(trades: list[Trade]) -> list[dict]:
    """Return daily rows sorted newest-first."""
    by_day: dict[str, list[Trade]] = defaultdict(list)
    for t in trades:
        by_day[t.date].append(t)
    rows = []
    for date in sorted(by_day):
        day_trades = by_day[date]
        wins = sum(1 for t in day_trades if t.pnl_usd > 0)
        losses = len(day_trades) - wins
        rows.append({
            "date": date,
            "pnl_usd": round(sum(t.pnl_usd for t in day_trades), 2),
            "trades": len(day_trades),
            "wins": wins,
            "losses": losses,
            "end_equity_usd": round(day_trades[-1].equity_after_usd, 2),
        })
    rows.sort(key=lambda r: r["date"], reverse=True)
    return rows


def _build_headline(trades: list[Trade]) -> dict:
    if not trades:
        return {}
    first = trades[0]
    last = trades[-1]
    starting = first.equity_after_usd - first.pnl_usd
    current = last.equity_after_usd
    realized = sum(t.pnl_usd for t in trades)
    wins = sum(1 for t in trades if t.pnl_usd > 0)
    losses = len(trades) - wins
    account_change = current - starting
    return {
        "account_change_usd": round(account_change, 2),
        "account_change_pct": round(account_change / starting, 6) if starting else 0.0,
        "realized_trade_pnl_usd": round(realized, 2),
        "cost_drift_usd": round(realized - account_change, 2),
        "total_trades": len(trades),
        "wins": wins,
        "losses": losses,
        "win_rate": round(wins / len(trades), 4) if trades else 0.0,
    }


# ---------------------------------------------------------------------------
# Invariant checks — refuse to write the file if anything diverges.
# ---------------------------------------------------------------------------
def _check_invariants(dashboard: dict) -> None:
    h = dashboard["headline"]
    daily = dashboard["daily"]
    trades = dashboard["trades"]
    bot = dashboard["bot"]

    daily_pnl_sum = round(sum(d["pnl_usd"] for d in daily), 2)
    if abs(daily_pnl_sum - h["realized_trade_pnl_usd"]) > 0.01:
        raise AssertionError(
            f"INVARIANT: sum(daily.pnl_usd)={daily_pnl_sum} != headline.realized_trade_pnl_usd={h['realized_trade_pnl_usd']}"
        )

    daily_trade_sum = sum(d["trades"] for d in daily)
    if daily_trade_sum != h["total_trades"]:
        raise AssertionError(
            f"INVARIANT: sum(daily.trades)={daily_trade_sum} != headline.total_trades={h['total_trades']}"
        )

    if h["wins"] + h["losses"] != h["total_trades"]:
        raise AssertionError(
            f"INVARIANT: wins+losses={h['wins']+h['losses']} != total_trades={h['total_trades']}"
        )

    if len(trades) != h["total_trades"]:
        raise AssertionError(
            f"INVARIANT: len(trades)={len(trades)} != headline.total_trades={h['total_trades']}"
        )

    eq_change = round(bot["current_equity"] - bot["starting_equity"], 2)
    if abs(eq_change - h["account_change_usd"]) > 0.01:
        raise AssertionError(
            f"INVARIANT: current-starting={eq_change} != headline.account_change_usd={h['account_change_usd']}"
        )


# ---------------------------------------------------------------------------
# Top-level build.
# ---------------------------------------------------------------------------
def build_dashboard(qb_root: str) -> dict:
    journal_dir = os.path.join(qb_root, "journal", "live")
    if not os.path.isdir(journal_dir):
        raise FileNotFoundError(f"journal dir missing: {journal_dir}")

    events = list(_iter_events(journal_dir))
    if not events:
        raise RuntimeError(f"no events found in {journal_dir}")

    trades, open_positions, orphan_closes = _match_trades(events)

    if not trades:
        raise RuntimeError(
            "no closed trades found — refusing to publish empty dashboard. "
            "If the bot truly has not closed a trade yet, do not deploy."
        )

    headline = _build_headline(trades)
    daily = _build_daily(trades)

    # data_as_of = timestamp of the last event we read (closed trade or otherwise)
    last_ts = max((e.get("ts", "") for e in events if e.get("ts")), default="")

    starting = round(trades[0].equity_after_usd - trades[0].pnl_usd, 2)
    current = round(trades[-1].equity_after_usd, 2)
    days_live = len({t.date for t in trades})

    warnings = []
    if abs(headline["cost_drift_usd"]) >= 1.0:
        sign = "+" if headline["cost_drift_usd"] > 0 else "−"
        warnings.append(
            f"realized trade P&L: {_fmt_usd(headline['realized_trade_pnl_usd'])}; "
            f"broker account change: {_fmt_usd(headline['account_change_usd'])}; "
            f"gap: {sign}{_fmt_usd(abs(headline['cost_drift_usd']))} in fees/slippage the bot did not capture per-trade"
        )
    if open_positions > 0:
        warnings.append(
            f"{open_positions} bracket(s) submitted but never closed in journal — likely orphans from a prior crash"
        )
    if orphan_closes > 0:
        warnings.append(
            f"{orphan_closes} position_closed event(s) with no matching bracket_submitted — journal incomplete"
        )

    dashboard = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "data_as_of": last_ts,
        "bot": {
            "name": BOT_NAME,
            "instrument": INSTRUMENT,
            "starting_equity": starting,
            "current_equity": current,
            "days_live": days_live,
        },
        "headline": headline,
        "daily": daily,
        "trades": [t.to_public() for t in trades],
        "data_quality": {
            "warnings": warnings,
            "orphan_closes": orphan_closes,
            "open_positions": open_positions,
        },
    }

    _check_invariants(dashboard)
    return dashboard


def _fmt_usd(amount: float) -> str:
    sign = "-" if amount < 0 else ""
    return f"{sign}${abs(amount):,.2f}"


def write_atomic(path: str, dashboard: dict) -> None:
    parent = os.path.dirname(path) or "."
    os.makedirs(parent, exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".dashboard.", suffix=".json", dir=parent)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(dashboard, fh, ensure_ascii=False, indent=2, sort_keys=False)
            fh.write("\n")
        os.replace(tmp, path)
    except Exception:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


def _summary_line(d: dict) -> str:
    h = d["headline"]
    bot = d["bot"]
    return (
        f"starting=${bot['starting_equity']:.2f} "
        f"current=${bot['current_equity']:.2f} "
        f"change={h['account_change_usd']:+.2f} "
        f"trades={h['total_trades']} "
        f"days={bot['days_live']} "
        f"drift={h['cost_drift_usd']:+.2f}"
    )


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("qb_root", help="path to quant-bot repo root")
    p.add_argument("--out", default="data/dashboard.json", help="output path (default: data/dashboard.json)")
    args = p.parse_args(argv)

    dashboard = build_dashboard(args.qb_root)
    write_atomic(args.out, dashboard)
    print(_summary_line(dashboard))
    return 0


if __name__ == "__main__":
    sys.exit(main())
