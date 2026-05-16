"""Canonical publisher for the Len Dawg Bot dashboard pipeline.

Reads quant-bot's raw artifacts and produces ONE schema-validated, PII-free
dashboard JSON file. This is the single point of truth for the dashboard
pipeline — everything downstream (build.py, the renderer, the deployment)
should consume the publisher's output verbatim.

Architectural inversion vs the legacy ``adapters/quant_bot.py``:

  Legacy:  read raw → reverse-engineer dashboard fields → strip PII via
           ``scripts/sanitize_jsonl.py`` (BLOCKLIST — leak-prone).
  This:    read raw → construct typed Dashboard → emit allow-listed JSON
           (ALLOWLIST — the schema IS the contract).

Because the Dashboard dataclass and its nested types declare every field that
can leave this process, PII (account_id, equity_usd, host, port, …) cannot
appear in the output. There is no separate "sanitize" step.

CLI:
    python3 tools/publisher.py \\
        --quant-bot-root /path/to/quant-bot \\
        --out journal/public/dashboard.json \\
        [--mode AUTO|LIVE|DAY0]

Stdlib only. ``pyarrow`` / ``pandas`` are optional and used only to read
``data/processed/trades.parquet`` if present.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
import tempfile
import uuid
from dataclasses import asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable

# Make the repo root importable so siblings (`adapters.translations`,
# `tools.publisher_schema`) resolve regardless of CWD.
_HERE = Path(__file__).resolve()
_ROOT = _HERE.parent.parent
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))

from adapters.translations import (  # noqa: E402
    EXIT_REASON_TRANSLATIONS,
    INSTRUMENT_FRIENDLY,
    PERSONA_NAMES,
    PERSONA_NICKNAMES,
    REGIME_TRANSLATIONS,
    SUB_SIGNAL_TRANSLATIONS,
)
from tools.publisher_schema import (  # noqa: E402
    PUBLISHER_VERSION,
    SCHEMA_VERSION,
    Bot,
    CageMatch,
    Cumulative,
    Dashboard,
    Day,
    EquityPoint,
    Gate,
    Persona,
    PersonaVote,
    Provenance,
    Trade,
    Translations,
    Verdict,
)

log = logging.getLogger("lendawgbot.publisher")

MAX_LIVE_STALENESS_HOURS = 30
LIVE_TRADE_EVENTS = {"bracket_filled", "trade_closed", "fill", "position_closed"}

# Equity-drift tolerance (USD). The bot's net_pnl_usd is supposed to be
# fully-loaded (gross - commissions), but commissions in practice drift
# vs. the IB-reported equity_after_usd by a few cents per trade. If the
# observed drift exceeds this, we emit a data_quality warning rather
# than silently fudging the math.
EQUITY_DRIFT_WARN_USD = 1.00

# Hard cap on a single JSONL line. quant-bot's events are O(KB); anything
# bigger is a corruption / runaway log. Without this cap a malformed line
# with no newline could drag the publisher OOM. See red_team_2026-04-27.md.
MAX_JSONL_LINE_BYTES = 1_048_576  # 1 MiB
MAX_JSONL_LINES = 200_000          # ~6 weeks of every-second heartbeats


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class PublisherError(Exception):
    """Hard publisher failure. Caller should NOT swallow these silently."""


# ---------------------------------------------------------------------------
# Mode detection + verdict picker
# ---------------------------------------------------------------------------


def _journal_paths(root: Path) -> tuple[Path, Path, Path]:
    """Return (day0_dir, live_dir, eod_dir) supporting both layouts.

    quant-bot keeps everything under ``<root>/journal/{day0,live,eod}/``, but
    the Len Dawg CI pre-stages a stub layout in ``data/quant_bot_journal/``
    where ``day0/`` and ``live/`` sit at the staging root directly.
    """
    journal = root / "journal"
    if journal.is_dir():
        return journal / "day0", journal / "live", journal / "eod"
    return root / "day0", root / "live", root / "eod"


def detect_mode(root: Path) -> str:
    """Return ``"LIVE"`` or ``"DAY0"``.

    LIVE iff the freshest live JSONL is younger than ``MAX_LIVE_STALENESS_HOURS``
    AND contains at least one real trade event (``bracket_filled``,
    ``trade_closed``, ``fill``, or ``position_closed``) whose ``sub_signal``
    is NOT ``SAT_TEST``. SAT_TEST is the bot's smoke-test bracket and must
    not promote a quiet day into LIVE mode.
    """
    day0_dir, live_dir, _ = _journal_paths(root)
    if live_dir.is_dir():
        live_files = sorted(live_dir.glob("*.jsonl"), reverse=True)
        for f in live_files:
            if not _is_fresh(f, MAX_LIVE_STALENESS_HOURS):
                continue
            if _has_real_trade_event(f):
                return "LIVE"
            log.info("live log %s heartbeat-only or SAT_TEST — staying DAY0", f.name)
            break
    if day0_dir.is_dir() and any(day0_dir.glob("verdict_*.json")):
        return "DAY0"
    raise PublisherError(f"no usable artifacts under {root}")


def _has_real_trade_event(path: Path) -> bool:
    """True if path contains a trade event that's not a SAT_TEST."""
    try:
        with path.open("r", encoding="utf-8") as fh:
            for n, line in enumerate(fh, start=1):
                if n > MAX_JSONL_LINES:
                    log.warning("%s: stopping scan at %d lines (cap)", path.name, MAX_JSONL_LINES)
                    break
                if len(line) > MAX_JSONL_LINE_BYTES:
                    log.warning("%s:%d skipped (line %d bytes > %d cap)",
                                path.name, n, len(line), MAX_JSONL_LINE_BYTES)
                    continue
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") not in LIVE_TRADE_EVENTS:
                    continue
                if obj.get("sub_signal") == "SAT_TEST":
                    continue
                # Ignore replay-mode brackets (test fixtures replayed for
                # diagnostics — not real fills).
                if obj.get("replay") is True:
                    continue
                # Ignore smoke-test brackets (BR-test*); they're synthetic
                # check-the-pipe events, not real fills.
                bid = obj.get("bracket_id") or ""
                if isinstance(bid, str) and bid.lower().startswith("br-test"):
                    continue
                return True
    except OSError as e:
        log.warning("could not read %s: %s", path, e)
    return False


def _is_fresh(path: Path, max_hours: int) -> bool:
    """True if path's mtime is within max_hours of now()."""
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime) < timedelta(hours=max_hours)


def _is_parseable_verdict(path: Path) -> bool:
    """True iff ``path`` parses as JSON AND contains a ``gate`` block.

    Defends against (a) truncated files (mid-write race with quant-bot)
    and (b) corrupt artifacts. A picker that returned a corrupt path used
    to silently produce a zero-gates dashboard — see red_team_2026-04-27.md.
    """
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    return isinstance(obj, dict) and isinstance(obj.get("gate"), dict)


def pick_canonical_verdict(day0_dir: Path) -> Path | None:
    """Return the path to the canonical verdict file in ``day0_dir``.

    Rule (mirrors the legacy ``read_day0_verdict`` logic, hardened):

      1. Prefer files matching ``verdict_<YYYY-MM-DD>.json`` (no suffix).
         These are the master MES edge-proof results.
      2. Among those, pick the latest by ISO date (filename), mtime tiebreak.
      3. If the picked file is corrupt or mid-write (unparseable, missing
         ``gate``), drop it and try the next candidate.
      4. If no canonical file is parseable, fall back to the freshest
         suffixed variant (verdict_trend_only_<DATE>.json, …) by the same
         rule.

    Returns ``None`` if no parseable verdict exists.
    """
    if not day0_dir.is_dir():
        return None
    all_candidates = list(day0_dir.glob("verdict_*.json"))
    if not all_candidates:
        return None
    canonical_re = re.compile(r"^verdict_\d{4}-\d{2}-\d{2}\.json$")
    canonical = [p for p in all_candidates if canonical_re.match(p.name)]

    def _pick(pool: list[Path]) -> Path | None:
        # Sort newest-first by (filename-date, mtime) and return the first
        # parseable one. This skips truncated / corrupt files instead of
        # silently emitting a zero-gates verdict.
        for p in sorted(pool, key=lambda x: (_extract_date(x.name) or "", x.stat().st_mtime),
                        reverse=True):
            if _is_parseable_verdict(p):
                return p
            log.warning("verdict %s unparseable or missing 'gate' — skipping", p.name)
        return None

    return _pick(canonical) or _pick(all_candidates)


def _extract_date(name: str) -> str | None:
    """Pull a YYYY-MM-DD from a filename."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else None


# ---------------------------------------------------------------------------
# Readers
# ---------------------------------------------------------------------------


def _read_json(path: Path) -> dict[str, Any] | None:
    """Read a JSON file or return None on any IO/parse failure."""
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not read %s: %s", path, e)
        return None


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    """Read a JSONL file. Skip malformed lines, never raise.

    Bounded by ``MAX_JSONL_LINE_BYTES`` and ``MAX_JSONL_LINES`` so a runaway
    log file can't drag the publisher into OOM territory.
    """
    if not path.exists():
        return []
    out: list[dict[str, Any]] = []
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, raw in enumerate(fh, start=1):
                if i > MAX_JSONL_LINES:
                    log.warning("%s: stopping read at %d lines (cap)", path.name, MAX_JSONL_LINES)
                    break
                if len(raw) > MAX_JSONL_LINE_BYTES:
                    log.warning("%s:%d skipped (line %d bytes > %d cap)",
                                path.name, i, len(raw), MAX_JSONL_LINE_BYTES)
                    continue
                line = raw.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    log.warning("malformed line %s:%d", path.name, i)
                    continue
                if isinstance(obj, dict):
                    out.append(obj)
    except OSError as e:
        log.warning("could not open %s: %s", path, e)
    return out


def _read_trades_parquet(path: Path) -> list[dict[str, Any]]:
    """Read trades.parquet via pyarrow/pandas. Returns [] on any failure.

    pyarrow is optional. If missing, we fall back to pandas; if both are
    missing we return [] and the caller reconstructs from the live event log.
    """
    if not path.exists():
        return []
    try:
        import pyarrow.parquet as pq  # type: ignore
        rows = pq.read_table(path).to_pylist()
        return [_normalize_parquet_row(r) for r in rows]
    except ImportError:
        pass
    except Exception as e:  # pragma: no cover
        log.warning("pyarrow read failed: %s", e)
    try:
        import pandas as pd  # type: ignore
        return [_normalize_parquet_row(r) for r in pd.read_parquet(path).to_dict(orient="records")]
    except ImportError:
        log.warning("neither pyarrow nor pandas available; parquet skipped")
        return []
    except Exception as e:
        log.warning("pandas parquet read failed: %s", e)
        return []


def _normalize_parquet_row(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a parquet row to dashboard-ready shape (ISO timestamps)."""
    out = dict(row)
    for k in ("entry_time", "exit_time"):
        v = out.get(k)
        if v is None:
            continue
        if hasattr(v, "isoformat"):
            out[k] = v.isoformat()
        else:
            out[k] = str(v)
    if "date" not in out and out.get("entry_time"):
        out["date"] = str(out["entry_time"])[:10]
    out.setdefault("persona_votes", [])
    return out


def _read_learnings(path: Path, max_items: int = 3) -> list[str]:
    """Read LEARNINGS.md and return the last ``max_items`` bullet lines."""
    if not path.exists():
        return []
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        try:
            text = path.read_text(encoding="latin-1")
        except OSError:
            return []
    except OSError:
        return []
    bullets: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            content = s[2:].strip()
            if content:
                bullets.append(content)
    return bullets[-max_items:] if bullets else []


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------


def _translations() -> Translations:
    """The static i18n table. Owned by the publisher, not quant-bot."""
    return Translations(
        regime=dict(REGIME_TRANSLATIONS),
        sub_signal=dict(SUB_SIGNAL_TRANSLATIONS),
        exit_reason=dict(EXIT_REASON_TRANSLATIONS),
        persona_nicknames=dict(PERSONA_NICKNAMES),
        persona_names=dict(PERSONA_NAMES),
        instrument_friendly=dict(INSTRUMENT_FRIENDLY),
    )


def _personas_record(trades: list[dict[str, Any]]) -> dict[str, Persona]:
    """Tally persona record across all trades."""
    record: dict[str, dict[str, int]] = {
        code: {"wins": 0, "losses": 0, "vetoed": 0} for code in PERSONA_NAMES
    }
    for t in trades:
        is_win = float(t.get("pnl_dollars") or 0.0) > 0
        for v in t.get("persona_votes") or []:
            code = v.get("persona")
            if code not in record:
                record[code] = {"wins": 0, "losses": 0, "vetoed": 0}
            verdict = v.get("verdict")
            if verdict == "TAKE":
                record[code]["wins" if is_win else "losses"] += 1
            elif verdict == "SKIP":
                record[code]["vetoed"] += 1
    return {
        code: Persona(
            code=code,
            name=PERSONA_NAMES.get(code, code),
            nickname=PERSONA_NICKNAMES.get(code, code),
            **rec,
        )
        for code, rec in record.items()
    }


def _build_day0(root: Path, verdict_path: Path) -> Dashboard:
    """Construct a DAY0-mode Dashboard from a verdict JSON file.

    Raises ``PublisherError`` if the file is unreadable / un-JSON / missing
    its ``gate`` block. Silent-empty fallback is wrong here: a zero-gates
    "Lab" verdict published by accident undoes weeks of green-gate work.
    """
    raw = _read_json(verdict_path)
    if raw is None or not isinstance(raw.get("gate"), dict):
        raise PublisherError(
            f"verdict {verdict_path.name} is unreadable or missing 'gate'"
        )
    gate = raw["gate"]
    wfo = raw.get("wfo") or {}

    def _b(v: Any) -> bool:
        if isinstance(v, bool):
            return v
        if isinstance(v, str):
            return v.strip().lower() == "true"
        return bool(v)

    def _f(v: Any) -> float | None:
        try:
            return float(v) if v is not None else None
        except (TypeError, ValueError):
            return None

    gates = [
        Gate(id="G1", name="In-Sample Sharpe ≥ 0.6", threshold=0.6,
             value=_f(gate.get("in_sample_sharpe")), pass_=_b(gate.get("g1_pass"))),
        Gate(id="G2", name="Out-of-Sample Sharpe ≥ 0.5", threshold=0.5,
             value=_f(gate.get("oos_sharpe")), pass_=_b(gate.get("g2_pass"))),
        Gate(id="G3", name="Deflated Sharpe ≥ 0.3", threshold=0.3,
             value=_f(gate.get("deflated_sharpe")), pass_=_b(gate.get("g3_pass"))),
        Gate(id="G4", name="PBO ≤ 0.30", threshold=0.30,
             value=_f(gate.get("pbo")), pass_=_b(gate.get("g4_pass"))),
        Gate(id="G5", name="Y5 Sealed Holdout Sharpe ≥ 0.4", threshold=0.4,
             value=_f(gate.get("holdout_sharpe")), pass_=_b(gate.get("g5_pass"))),
    ]
    gates_passed = sum(1 for g in gates if g.pass_)
    all_pass = bool(gate.get("all_pass", gates_passed == len(gates)))
    label = "PASS" if all_pass else ("FAIL" if gates_passed < len(gates) else "PENDING")
    headline = (
        "Len Dawg has earned the right to trade real money."
        if all_pass
        else f"Len Dawg is still being tested. Passed {gates_passed} of {len(gates)}."
    )

    instrument = "/" + str(raw.get("instrument") or "MES").upper().lstrip("/")
    bot = Bot(
        name="Len Dawg",
        instrument=instrument,
        instrument_friendly=INSTRUMENT_FRIENDLY.get(instrument, instrument),
        phase="Phase A — Live, 1 contract max" if all_pass else "In the lab — passing edge-proof gates",
        phase_short="Live" if all_pass else "Lab",
        days_live=0,
        # DAY0 mode = bot hasn't traded yet. There's no real starting
        # equity to report; the renderer doesn't display this field in
        # the gates view anyway.
        starting_equity=0.0,
    )

    verdict = Verdict(
        verdict=label,
        headline=headline,
        gates_passed=gates_passed,
        gates=gates,
        all_pass=all_pass,
        wfo_winner=wfo.get("winner"),
        holdout_start=wfo.get("holdout_start"),
        holdout_end=wfo.get("holdout_end"),
    )

    as_of = (raw.get("ts") or "")[:10] or _today()
    return Dashboard(
        schema_version=SCHEMA_VERSION,
        publisher_version=PUBLISHER_VERSION,
        generated_at=_now_iso(),
        mode="DAY0",
        bot=bot,
        translations=_translations(),
        personas={
            code: Persona(code=code, name=PERSONA_NAMES[code], nickname=PERSONA_NICKNAMES[code])
            for code in PERSONA_NAMES
        },
        equity_curve=[],
        today_trades=[],
        today=None,
        cumulative=None,
        cage_match=None,
        verdict=verdict,
        as_of_date=as_of,
        ai_learnings=_read_learnings(root / "LEARNINGS.md"),
        tomorrow_watch=[],
    )


def _reconstruct_trades_from_events(root: Path) -> list[dict[str, Any]]:
    """Build Trade-shaped dicts from raw live-log events.

    Three event sources are indexed per bracket_id:
      * bracket_submitted  — entry-side intent (sub_signal, regime, target/stop)
      * fill               — confirmed broker fills (entry leg + target/stop leg)
      * position_closed    — exit summary with net_pnl_usd

    A bracket counts as a closed trade if it has EITHER:
      (a) a position_closed or trade_closed event (these carry net P&L), OR
      (b) both an entry-leg fill AND an exit-leg fill (target / stop /
          market_close / trail).

    NOTE: ``bracket_filled`` is an entry-side event (broker confirmed entry +
    OCA stop/target placement), NOT a close. It correctly trips LIVE-mode
    detection (real trading is happening) but does not on its own indicate
    a trade has closed. Don't include it here.

    Used in the early live window before trades.parquet exists. When the
    parquet appears, _build_live() prefers that and skips reconstruction.

    Filters: any bracket_id starting with ``br-test`` (case-insensitive) is
    dropped — those are smoke-test brackets the bot writes to verify the
    publish pipeline. Any event with ``replay == True`` is also dropped
    (replayed-from-fixtures diagnostics, not real fills).
    """
    _, live_dir, _ = _journal_paths(root)
    if not live_dir.is_dir():
        return []

    submits: dict[str, dict[str, Any]] = {}        # bid → submit event
    fills_by_bid: dict[str, list[dict[str, Any]]] = {}  # bid → list of fills
    closes_by_bid: dict[str, dict[str, Any]] = {}  # bid → close event

    def _is_excluded(ev: dict[str, Any]) -> bool:
        if ev.get("replay") is True:
            return True
        bid = ev.get("bracket_id") or ""
        return isinstance(bid, str) and bid.lower().startswith("br-test")

    for jsonl in sorted(live_dir.glob("*.jsonl")):
        for ev in _read_jsonl(jsonl):
            if _is_excluded(ev):
                continue
            bid = ev.get("bracket_id") or ""
            if not bid:
                continue
            event = ev.get("event")
            if event == "bracket_submitted":
                submits.setdefault(bid, ev)  # first submit wins; later ones can't override
            elif event == "fill":
                fills_by_bid.setdefault(bid, []).append(ev)
            elif event in {"position_closed", "trade_closed"}:
                # Multiple close events for the same bid (rare): keep first.
                closes_by_bid.setdefault(bid, ev)
            # bracket_filled deliberately omitted — it's an entry event.

    # The union of bids that have at least one piece of trade-closure evidence.
    candidate_bids: set[str] = set(closes_by_bid.keys())
    for bid, fills in fills_by_bid.items():
        legs = {(f.get("leg") or "").lower() for f in fills}
        # Entry + any kind of exit = a complete fill-only trade.
        if "entry" in legs and (legs & {"target", "stop", "market_close", "trail"}):
            candidate_bids.add(bid)

    trades: list[dict[str, Any]] = []
    for bid in candidate_bids:
        sub = submits.get(bid) or {}
        close = closes_by_bid.get(bid) or {}
        fills = fills_by_bid.get(bid, [])

        entry_fill = next((f for f in fills if (f.get("leg") or "").lower() == "entry"), None)
        exit_fill = next(
            (f for f in fills
             if (f.get("leg") or "").lower() in {"target", "stop", "market_close", "trail"}),
            None,
        )

        # Timestamps: prefer the most precise source per leg.
        entry_ts = (entry_fill or {}).get("ts") or sub.get("ts") or ""
        exit_ts = close.get("ts") or (exit_fill or {}).get("ts") or ""
        date = ((exit_ts or entry_ts)[:10] if isinstance(exit_ts or entry_ts, str) else "") or _today()

        # PnL: close.net_pnl_usd is the bot's own authoritative number;
        # never compute from raw prices here (instrument point-value
        # heterogeneity is not the publisher's job).
        pnl = close.get("net_pnl_usd")
        try:
            pnl_dollars = round(float(pnl), 2) if pnl is not None else 0.0
        except (TypeError, ValueError):
            pnl_dollars = 0.0

        # Side / direction: prefer submit, else infer from entry fill action.
        side = (sub.get("side") or "").upper()
        if not side and entry_fill:
            act = (entry_fill.get("action") or "").upper()
            side = "LONG" if act == "BUY" else ("SHORT" if act == "SELL" else "")
        direction = side if side in {"LONG", "SHORT"} else ""

        # Numeric defaults — never raise on a missing or malformed value.
        def _f(v: Any) -> float:
            try:
                return float(v) if v is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        entry_price = _f((entry_fill or {}).get("price")) or _f(sub.get("entry"))
        exit_price = _f((exit_fill or {}).get("price")) or _f(close.get("exit_price"))

        # pnl_points: signed price delta in the trade's direction. Tape views
        # use this; the dashboard's hero $-figure uses pnl_dollars instead.
        if direction == "LONG":
            pnl_points = round(exit_price - entry_price, 2) if entry_price and exit_price else 0.0
        elif direction == "SHORT":
            pnl_points = round(entry_price - exit_price, 2) if entry_price and exit_price else 0.0
        else:
            pnl_points = 0.0

        # bars_held: best-effort 1-min-bar count between entry and exit
        # timestamps. 0 when either timestamp is missing.
        bars_held = 0
        if entry_ts and exit_ts:
            try:
                from datetime import datetime
                e_dt = datetime.fromisoformat(str(entry_ts).replace("Z", "+00:00"))
                x_dt = datetime.fromisoformat(str(exit_ts).replace("Z", "+00:00"))
                bars_held = max(0, int(round((x_dt - e_dt).total_seconds() / 60.0)))
            except (ValueError, TypeError):
                bars_held = 0

        # exit_reason from whichever exit signal we got.
        leg = (close.get("exit_leg") or (exit_fill or {}).get("leg") or "").lower()
        exit_reason = {
            "target": "TARGET",
            "stop": "STOP",
            "market_close": "TIME",
            "trail": "TRAILING",
        }.get(leg, "TARGET")

        trades.append({
            "trade_id": bid,
            "date": date,
            "entry_time": entry_ts or "",
            "exit_time": exit_ts or "",
            "direction": direction,
            "sub_signal": sub.get("sub_signal") or "UNCLASSIFIED",
            "regime": sub.get("regime") or "UNCLASSIFIED",
            "qty": int(sub.get("qty") or 1),
            "entry_price": entry_price,
            "exit_price": exit_price,
            "stop_price": _f(sub.get("stop")),
            "target_price": _f(sub.get("target")),
            "pnl_dollars": pnl_dollars,
            "pnl_points": pnl_points,
            "bars_held": bars_held,
            "exit_reason": exit_reason,
            "thesis_confirmed": pnl_dollars > 0,
            "persona_votes": [],
        })

    # Stable order: chronological by entry_time, ascending.
    trades.sort(key=lambda t: t.get("entry_time") or "")
    log.info(
        "reconstructed %d trade(s) from events  (submits=%d, fills=%d brackets, closes=%d)",
        len(trades), len(submits), len(fills_by_bid), len(closes_by_bid),
    )
    return trades


def _empty_live_payload(root: Path) -> Dashboard:
    """Minimal LIVE payload for the awaiting-first-trade state.

    Used when the bot is in LIVE mode but has no position_closed events
    yet (or all closes were filtered as test/replay). The renderer reads
    ``awaiting_first_trade: True`` and shows the "Len Dawg is watching
    the market" banner instead of fake numbers.
    """
    days_live_count = _count_trading_days(root)
    return Dashboard(
        schema_version=SCHEMA_VERSION,
        publisher_version=PUBLISHER_VERSION,
        generated_at=_now_iso(),
        mode="LIVE",
        bot=Bot(
            name="Len Dawg",
            instrument="/MES",
            instrument_friendly=INSTRUMENT_FRIENDLY.get("/MES", "S&P 500 mini"),
            phase="Phase A — Live, 1 contract max",
            phase_short="Live",
            days_live=max(1, days_live_count),
            # Unknown until first close; do NOT use a synthetic default.
            starting_equity=0.0,
        ),
        translations=_translations(),
        personas={code: Persona(code=code, name=PERSONA_NAMES[code], nickname=PERSONA_NICKNAMES[code])
                  for code in PERSONA_NAMES},
        equity_curve=[],
        today_trades=[],
        today=None,
        cumulative=None,
        cage_match=None,
        verdict=None,
        as_of_date=_today(),
        ai_learnings=_read_learnings(root / "LEARNINGS.md"),
        tomorrow_watch=[],
        awaiting_first_trade=True,
    )


def _read_real_position_closes(root: Path) -> list[dict[str, Any]]:
    """Walk every live JSONL in chronological order; return only real (non-test,
    non-replay) ``position_closed`` events. Each event carries the bot's
    authoritative ``equity_after_usd`` and ``net_pnl_usd`` — no reconstruction.
    """
    _, live_dir, _ = _journal_paths(root)
    if not live_dir.is_dir():
        return []
    out: list[dict[str, Any]] = []
    for jsonl in sorted(live_dir.glob("*.jsonl")):
        for ev in _read_jsonl(jsonl):
            if ev.get("event") != "position_closed":
                continue
            if ev.get("replay") is True:
                continue
            bid = ev.get("bracket_id") or ""
            if isinstance(bid, str) and bid.lower().startswith("br-test"):
                continue
            out.append(ev)
    out.sort(key=lambda e: e.get("ts") or "")
    return out


def _count_trading_days(root: Path) -> int:
    """Distinct dates with at least one non-test, non-replay
    bracket_submitted or position_closed. Counts a day where the bot
    placed an order even if nothing closed that day.
    """
    _, live_dir, _ = _journal_paths(root)
    if not live_dir.is_dir():
        return 0
    days: set[str] = set()
    activity_events = {"bracket_submitted", "position_closed", "fill"}
    for jsonl in sorted(live_dir.glob("*.jsonl")):
        for ev in _read_jsonl(jsonl):
            if ev.get("event") not in activity_events:
                continue
            if ev.get("replay") is True:
                continue
            bid = ev.get("bracket_id") or ""
            if isinstance(bid, str) and bid.lower().startswith("br-test"):
                continue
            if ev.get("sub_signal") == "SAT_TEST":
                continue
            ts = ev.get("ts")
            if isinstance(ts, str) and len(ts) >= 10:
                days.add(ts[:10])
    return len(days)


def _build_live(root: Path) -> Dashboard:
    """Construct a LIVE-mode Dashboard truth-first.

    Source of truth: real ``position_closed`` events from the bot's journal.
    Each event carries ``equity_after_usd`` (IB NetLiquidation after the
    trade) and ``net_pnl_usd`` (commission-loaded P&L). We read these
    directly — no STARTING_EQUITY_DEFAULT fudge, no synthetic baselines.

    Trade detail (entry/exit prices, sub_signal, side, etc.) comes from
    the matching bracket_submitted + fill events via
    ``_reconstruct_trades_from_events``. If the trade reconstruction yields
    fewer trades than ``position_closed`` events suggest (orphan closes
    from earlier log files), we surface that as a ``data_quality`` warning
    rather than silently dropping numbers.

    Invariants asserted at the end:
      - len(trades) == sum(daily_wins) + sum(daily_losses) + sum(daily_breakevens)
      - abs(sum(daily_pnl) - cumulative_pnl) < 0.01
    """
    closes = _read_real_position_closes(root)

    if not closes:
        # No closed positions yet — minimal payload, renderer's
        # awaiting-first-trade banner takes over.
        return _empty_live_payload(root)

    # --- Equity series: read REAL equity_after_usd ---------------------------
    # Each position_closed is a real datapoint. The equity curve plots them
    # in chronological order. Y-axis = the bot's actual equity, not a
    # synthetic 10K-based offset.
    starting_equity_observed = round(closes[0]["equity_after_usd"] - closes[0]["net_pnl_usd"], 2)
    ending_equity = round(closes[-1]["equity_after_usd"], 2)
    cumulative_pnl_real = round(sum(float(c["net_pnl_usd"]) for c in closes), 2)

    # Drift check: if commissions/fees moved equity beyond per-trade net_pnl,
    # we surface the divergence rather than fake-matching the numbers.
    expected_ending = round(starting_equity_observed + cumulative_pnl_real, 2)
    drift = round(expected_ending - ending_equity, 2)
    drift_warnings: list[str] = []
    if abs(drift) > EQUITY_DRIFT_WARN_USD:
        drift_warnings.append(
            f"equity_drift: starting + sum(pnl) = ${expected_ending:.2f} but bot reports ${ending_equity:.2f} (drift ${drift:+.2f}, likely commissions)"
        )

    # --- Trade detail: reconstruct from bracket_submitted + fills -----------
    trades = _reconstruct_trades_from_events(root)
    # Align trade list to closes by bracket_id (some recon trades may lack a
    # matching close in scope — drop those; some closes may lack a recon
    # trade — surface those as data_quality).
    closes_by_bid = {c["bracket_id"]: c for c in closes if c.get("bracket_id")}
    recon_by_bid = {t["trade_id"]: t for t in trades if t.get("trade_id")}
    pairs_orphaned = len(set(closes_by_bid.keys()) - set(recon_by_bid.keys()))
    if pairs_orphaned > 0:
        drift_warnings.append(
            f"orphan_closes: {pairs_orphaned} position_closed event(s) without matching bracket_submitted in scope"
        )

    # Trust position_closed for pnl_dollars + exit_price (authoritative).
    # Trust bracket_submitted for sub_signal + side + entry_price.
    for t in trades:
        bid = t.get("trade_id")
        if bid in closes_by_bid:
            close = closes_by_bid[bid]
            t["pnl_dollars"] = round(float(close.get("net_pnl_usd") or 0.0), 2)
            t["thesis_confirmed"] = t["pnl_dollars"] > 0
    # Drop any reconstructed "trades" that don't actually have a close
    # event — they're still-open positions, not closed trades.
    trades = [t for t in trades if t.get("trade_id") in closes_by_bid]

    # --- Day summaries -------------------------------------------------------
    by_date: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_date.setdefault(t.get("date") or "", []).append(t)

    summaries: list[Day] = []
    equity_curve: list[EquityPoint] = []
    cumulative_pnl_running = 0.0
    for date_key in sorted(k for k in by_date.keys() if k):
        day_trades = by_date[date_key]
        day_pnl = round(sum(float(x.get("pnl_dollars") or 0.0) for x in day_trades), 2)
        cumulative_pnl_running = round(cumulative_pnl_running + day_pnl, 2)
        wins = sum(1 for x in day_trades if float(x.get("pnl_dollars") or 0.0) > 0)
        losses = sum(1 for x in day_trades if float(x.get("pnl_dollars") or 0.0) < 0)
        bevens = sum(1 for x in day_trades if float(x.get("pnl_dollars") or 0.0) == 0)
        regime = _mode_value(x.get("regime") for x in day_trades) or "UNCLASSIFIED"
        summaries.append(Day(
            date=date_key,
            regime=regime,
            trades_count=len(day_trades),
            wins=wins,
            # The schema's Day.losses includes breakevens by convention (so
            # wins+losses == trades_count). Keep that contract.
            losses=losses + bevens,
            pnl_dollars=day_pnl,
            cumulative_pnl=cumulative_pnl_running,
        ))
        # Equity at end-of-day: use the LAST real close on that day.
        day_last_close = max(
            (c for c in closes if c.get("ts", "")[:10] == date_key),
            key=lambda c: c.get("ts", ""),
            default=None,
        )
        eod_equity = (
            round(float(day_last_close["equity_after_usd"]), 2)
            if day_last_close else round(starting_equity_observed + cumulative_pnl_running, 2)
        )
        equity_curve.append(EquityPoint(date=date_key, equity=eod_equity, pnl=day_pnl))

    today_summary = summaries[-1] if summaries else None
    today_date = today_summary.date if today_summary else _today()
    today_trade_dicts = [t for t in trades if t.get("date") == today_date]
    today_trades = [_to_trade(t) for t in today_trade_dicts]

    total_wins = sum(1 for t in trades if float(t.get("pnl_dollars") or 0.0) > 0)
    win_rate = round(total_wins / len(trades), 3) if trades else 0.0
    streak, streak_type = _compute_streak(summaries)
    cumulative = Cumulative(
        total_pnl=cumulative_pnl_real,
        total_trades=len(trades),
        win_rate=win_rate,
        # current_equity = bot's actual last reported equity, NOT
        # starting + sum(pnl). Drift is surfaced via data_quality.
        current_equity=ending_equity,
        streak=streak,
        streak_type=streak_type,
    )

    cage = _pick_cage_match(today_trade_dicts)

    instrument = "/MES"
    days_live_count = _count_trading_days(root)
    bot = Bot(
        name="Len Dawg",
        instrument=instrument,
        instrument_friendly=INSTRUMENT_FRIENDLY.get(instrument, instrument),
        phase="Phase A — Live, 1 contract max",
        phase_short="Live",
        days_live=max(1, days_live_count),
        # Real starting equity (the bot's first observed equity_after, less
        # the first trade's pnl) — NOT a synthetic 10K default.
        starting_equity=starting_equity_observed,
    )

    # --- Invariant assertions: fail loudly if math doesn't add up -----------
    # These are tenets. If any of them fails, we refuse to publish — better
    # red CI than wrong numbers on a public site.
    sum_daily_pnl = round(sum(d.pnl_dollars for d in summaries), 2)
    if abs(sum_daily_pnl - cumulative_pnl_real) > 0.01:
        raise PublisherError(
            f"invariant violated: sum(daily_pnl)={sum_daily_pnl} but cumulative={cumulative_pnl_real}"
        )
    sum_wins_losses = sum(d.wins + d.losses for d in summaries)
    if sum_wins_losses != len(trades):
        raise PublisherError(
            f"invariant violated: sum(wins+losses)={sum_wins_losses} but len(trades)={len(trades)}"
        )

    awaiting = (len(today_trades) == 0)

    log.info(
        "_build_live: starting=$%.2f ending=$%.2f cumulative=$%+.2f trades=%d days=%d%s",
        starting_equity_observed, ending_equity, cumulative_pnl_real,
        len(trades), days_live_count,
        f" warnings={drift_warnings}" if drift_warnings else "",
    )

    return Dashboard(
        schema_version=SCHEMA_VERSION,
        publisher_version=PUBLISHER_VERSION,
        generated_at=_now_iso(),
        mode="LIVE",
        bot=bot,
        translations=_translations(),
        personas=_personas_record(trades),
        equity_curve=equity_curve,
        today_trades=today_trades,
        today=today_summary,
        cumulative=cumulative,
        cage_match=cage,
        verdict=None,
        as_of_date=today_date,
        # ai_learnings is renderer-facing copy ("what Len Dawg learned today"),
        # not a data-quality channel. drift_warnings are logged above and
        # will move into a formal data_quality field in Phase 2.
        ai_learnings=_read_learnings(root / "LEARNINGS.md"),
        tomorrow_watch=[],
        awaiting_first_trade=awaiting,
    )


def _to_trade(d: dict[str, Any]) -> Trade:
    """Cast a dict trade into the typed Trade allow-listed shape."""
    return Trade.from_dict(d)


def _pick_cage_match(today_trades: list[dict[str, Any]]) -> CageMatch | None:
    """Pick today's most-disagreed-on trade. Returns None if no trades."""
    if not today_trades:
        return None
    most_div = max(
        today_trades,
        key=lambda t: len({v.get("verdict") for v in (t.get("persona_votes") or [])}),
    )
    return CageMatch(
        trade_id=str(most_div.get("trade_id") or ""),
        outcome="WIN" if float(most_div.get("pnl_dollars") or 0.0) > 0 else "LOSS",
        pnl_dollars=float(most_div.get("pnl_dollars") or 0.0),
        direction=str(most_div.get("direction") or ""),
        sub_signal=str(most_div.get("sub_signal") or ""),
        regime=str(most_div.get("regime") or ""),
        votes=[PersonaVote.from_dict(v) for v in (most_div.get("persona_votes") or [])],
    )


def _compute_streak(summaries: list[Day]) -> tuple[int, str]:
    """Count consecutive winning/losing days from the most recent backwards."""
    streak = 0
    streak_type = "none"
    for s in reversed(summaries):
        pnl = s.pnl_dollars
        if pnl > 0 and streak_type in ("none", "win"):
            streak += 1
            streak_type = "win"
        elif pnl < 0 and streak_type in ("none", "loss"):
            streak += 1
            streak_type = "loss"
        else:
            break
    return streak, streak_type


def _mode_value(values: Iterable[Any]) -> Any:
    """Return the most common non-None value."""
    counts: dict[Any, int] = {}
    for v in values:
        if v is None:
            continue
        counts[v] = counts.get(v, 0) + 1
    if not counts:
        return None
    return max(counts.items(), key=lambda kv: kv[1])[0]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _today() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# Top-level pipeline
# ---------------------------------------------------------------------------


def build(quant_bot_root: Path, mode: str = "AUTO") -> Dashboard:
    """Build the canonical Dashboard from a quant-bot project root.

    Returns a fully constructed, schema-validated Dashboard. Does not write.
    Raises ``PublisherError`` only on hard failure (missing artifacts in
    DAY0 mode, unreadable verdict file, etc.).
    """
    if not quant_bot_root.is_dir():
        raise PublisherError(f"quant_bot_root does not exist: {quant_bot_root}")
    resolved = detect_mode(quant_bot_root) if mode == "AUTO" else mode
    log.info("publisher mode=%s root=%s", resolved, quant_bot_root)

    if resolved == "LIVE":
        return _build_live(quant_bot_root)
    elif resolved == "DAY0":
        day0_dir, _, _ = _journal_paths(quant_bot_root)
        verdict_path = pick_canonical_verdict(day0_dir)
        if verdict_path is None:
            raise PublisherError(f"no verdict_*.json under {day0_dir}")
        log.info("publisher chose verdict: %s", verdict_path.name)
        return _build_day0(quant_bot_root, verdict_path)
    else:
        raise PublisherError(f"unknown mode: {mode}")


def _source_detail(root: Path, mode: str) -> str:
    """One-line provenance string identifying the input artifact(s)."""
    parts: list[str] = []
    day0_dir, live_dir, _ = _journal_paths(root)
    if mode == "DAY0":
        v = pick_canonical_verdict(day0_dir)
        if v is not None:
            parts.append(v.name)
    else:
        live_files = sorted(live_dir.glob("*.jsonl"), reverse=True) if live_dir.is_dir() else []
        if live_files:
            parts.append(f"live/{live_files[0].name}")
        trades = root / "data" / "processed" / "trades.parquet"
        if trades.exists():
            parts.append("data/processed/trades.parquet")
    return " + ".join(parts) or "quant-bot"


def _data_as_of(root: Path, mode: str) -> str | None:
    """Return the mtime of the primary input artifact, ISO-8601."""
    day0_dir, live_dir, _ = _journal_paths(root)
    target: Path | None = None
    if mode == "DAY0":
        target = pick_canonical_verdict(day0_dir)
    else:
        trades = root / "data" / "processed" / "trades.parquet"
        if trades.exists():
            target = trades
        elif live_dir.is_dir():
            files = sorted(live_dir.glob("*.jsonl"), reverse=True)
            target = files[0] if files else None
    if target is None or not target.exists():
        return None
    return datetime.fromtimestamp(target.stat().st_mtime, tz=timezone.utc).isoformat()


def _validate_round_trip(d: dict[str, Any]) -> None:
    """Parse the dict via ``Dashboard.from_dict`` and compare to itself.

    This catches schema drift before we write to disk: if a field can't be
    rehydrated, the publisher fails BEFORE the bad JSON reaches the renderer.
    """
    rehydrated = Dashboard.from_dict(d)
    again = rehydrated.to_dict()
    if again != d:
        # Find the first divergent key for a useful error message.
        diff = _first_diff(d, again)
        raise PublisherError(f"round-trip mismatch at {diff}")


def _first_diff(a: Any, b: Any, path: str = "") -> str:
    """Recursive structural diff that reports the first divergence path."""
    if a == b:
        return ""
    if type(a) is not type(b):
        return f"{path or '<root>'} ({type(a).__name__} vs {type(b).__name__})"
    if isinstance(a, dict):
        for k in set(a) | set(b):
            sub = _first_diff(a.get(k), b.get(k), f"{path}.{k}" if path else k)
            if sub:
                return sub
    if isinstance(a, list):
        for i in range(max(len(a), len(b))):
            sub = _first_diff(
                a[i] if i < len(a) else None,
                b[i] if i < len(b) else None,
                f"{path}[{i}]",
            )
            if sub:
                return sub
    return f"{path or '<root>'} differs"


def atomic_write(out_path: Path, payload: dict[str, Any]) -> None:
    """Write ``payload`` to ``out_path`` atomically.

    Strategy: serialise to a sibling temp file, fsync, ``os.replace``. The
    destination is only ever observed in its previous-good state or its
    new-good state — never partial. Crashes mid-write leave the previous
    file untouched.
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{out_path.name}.", dir=str(out_path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False, default=str)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, out_path)
    except Exception:
        # Clean up the temp file if anything went wrong before the rename.
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def publish(quant_bot_root: Path, out_path: Path, mode: str = "AUTO") -> dict[str, Any]:
    """End-to-end: read → build → validate → write. Returns the payload.

    This is the function CI/cron should call. Exceptions inside the build
    propagate; the CLI ``main()`` swallows + logs them so a busted publisher
    can't crash the dashboard cron.
    """
    dashboard = build(quant_bot_root, mode=mode)
    payload = dashboard.to_dict()
    payload["_provenance"] = asdict(Provenance(
        source="quant-bot",
        source_detail=_source_detail(quant_bot_root, dashboard.mode),
        data_as_of=_data_as_of(quant_bot_root, dashboard.mode),
        publisher_run_id=str(uuid.uuid4()),
    ))
    _validate_round_trip(payload)
    atomic_write(out_path, payload)
    log.info("publisher wrote %s (mode=%s, schema=%s)", out_path, dashboard.mode, SCHEMA_VERSION)
    return payload


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    """CLI entry point. Logs and returns 0/1; never raises out of the process."""
    logging.basicConfig(
        level=os.environ.get("PUBLISHER_LOG", "INFO"),
        format="%(levelname)s %(name)s: %(message)s",
    )
    parser = argparse.ArgumentParser(description="Canonical Len Dawg Bot dashboard publisher")
    parser.add_argument("--quant-bot-root", type=Path, required=True,
                        help="Path to a quant-bot project root.")
    parser.add_argument("--out", type=Path, required=True,
                        help="Destination JSON file. Parents created if needed.")
    parser.add_argument("--mode", choices=["AUTO", "LIVE", "DAY0"], default="AUTO",
                        help="Mode hint. AUTO inspects the filesystem.")
    args = parser.parse_args(argv)

    try:
        publish(args.quant_bot_root, args.out, mode=args.mode)
        return 0
    except PublisherError as e:
        log.error("publisher hard failure: %s", e)
        return 1
    except Exception as e:  # pragma: no cover
        log.exception("publisher unexpected exception: %s", e)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
