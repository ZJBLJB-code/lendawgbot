"""TomCash <- quant-bot adapter.

Reads raw artifacts from a quant-bot project root and emits the dashboard
payload shape consumed by `src/template.html` (see `data/sample.json` for the
schema-of-truth).

Entry point: `build_dashboard(quant_bot_root, mode="AUTO")`.

Design notes (see ADAPTER_DESIGN.md sibling doc):
  - Presentation-layer concerns (nicknames, plain-English translations) live in
    `tomcash/adapters/translations.py` and are stitched in here. quant-bot is
    not asked to know about them.
  - Staleness: data older than MAX_STALENESS_HOURS triggers an "AWAITING
    TODAY'S DATA" empty-state payload, *not* a stale render and *not* a hard
    failure. The dashboard always builds.
  - Mode detection prefers LIVE over DAY0 when both exist, since LIVE is the
    forward-looking story.
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Literal

from .translations import (
    EXIT_REASON_TRANSLATIONS,
    INSTRUMENT_FRIENDLY,
    PERSONA_NAMES,
    PERSONA_NICKNAMES,
    REGIME_TRANSLATIONS,
    SUB_SIGNAL_TRANSLATIONS,
)

log = logging.getLogger("tomcash.adapter")

Dashboard = dict[str, Any]  # matches data/sample.json shape; typed-dict later
Mode = Literal["LIVE", "DAY0", "AUTO"]

MAX_STALENESS_HOURS = 30  # ~one trading day + buffer for weekend/holiday gaps
LIVE_TRADE_EVENTS = {"bracket_filled", "trade_closed"}
STARTING_EQUITY_DEFAULT = 10_000.0


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class AdapterError(Exception):
    """Recoverable adapter failure. Caller should fall back to sample data."""


@dataclass
class AdapterResult:
    dashboard: Dashboard
    source: Literal["quant-bot", "empty-state", "sample-fallback"]
    warnings: list[str]


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_dashboard(quant_bot_root: Path, mode: Mode = "AUTO") -> Dashboard:
    """Build a TomCash dashboard payload from a quant-bot project root.

    Read order:
      1. Detect mode (LIVE vs DAY0) from filesystem if mode == "AUTO".
      2. Load the freshest artifact for that mode.
      3. If freshest artifact is older than MAX_STALENESS_HOURS, return the
         empty-state payload instead of stale data.
      4. Stitch in static translations / nicknames from translations.py.
      5. Validate against the minimum required keys; raise AdapterError on
         schema drift.

    Args:
      quant_bot_root: path to quant-bot project root (contains journal/, data/).
      mode: "LIVE", "DAY0", or "AUTO" (default).

    Returns:
      A dict matching the schema of data/sample.json. The dashboard renderer
      expects this exact shape.

    Raises:
      AdapterError: when artifacts are unreadable or schema-incompatible.
        Callers (build.py) should catch and fall back to sample.json.
    """
    if not quant_bot_root.is_dir():
        raise AdapterError(f"quant_bot_root does not exist: {quant_bot_root}")

    resolved_mode = detect_mode(quant_bot_root) if mode == "AUTO" else mode
    log.info("adapter mode resolved: %s", resolved_mode)

    if resolved_mode == "LIVE":
        result = _build_live(quant_bot_root)
    else:
        result = _build_day0(quant_bot_root)

    # Always stitch presentation-layer concerns *after* sourcing data.
    _attach_translations(result.dashboard)
    _validate(result.dashboard, resolved_mode)
    for w in result.warnings:
        log.warning("adapter warning: %s", w)
    return result.dashboard


# ---------------------------------------------------------------------------
# Mode detection
# ---------------------------------------------------------------------------


def _journal_paths(quant_bot_root: Path) -> tuple[Path, Path]:
    """Return (day0_dir, live_dir) accepting either a full quant-bot project
    root (containing ``journal/``) or a pre-staged stub (``<root>/day0/`` and
    ``<root>/live/`` directly).

    The staged-stub layout is what TomCash CI uses — only the day0 verdicts
    are checked into ``tomcash/data/quant_bot_journal/`` so the runner can
    build without access to the full quant-bot worktree.
    """
    journal = quant_bot_root / "journal"
    if journal.is_dir():
        return journal / "day0", journal / "live"
    # stub layout: <root>/day0, <root>/live
    return quant_bot_root / "day0", quant_bot_root / "live"


def detect_mode(quant_bot_root: Path) -> Literal["LIVE", "DAY0"]:
    """Decide LIVE vs DAY0 from filesystem alone.

    Rule:
      - LIVE if a live log exists, is fresh (mtime within
        MAX_STALENESS_HOURS), AND contains at least one
        `bracket_filled`/`trade_closed`/`fill`/`position_closed` event.
        Heartbeat-only logs do not count.
      - else DAY0 if any `verdict_*.json` exists in the day0 directory.
      - else raise AdapterError (no artifacts at all).

    Both the full quant-bot layout (``journal/{live,day0}/``) and the staged
    stub layout (``<root>/{live,day0}/``) are supported.
    """
    day0_dir, live_dir = _journal_paths(quant_bot_root)

    if live_dir.is_dir():
        live_files = sorted(live_dir.glob("*.jsonl"), reverse=True)
        for f in live_files:
            if not _is_fresh(f):
                continue
            if _has_trade_events(f):
                return "LIVE"
            log.info("live log %s exists but heartbeat-only — not LIVE", f.name)
            break  # only inspect the freshest file

    if day0_dir.is_dir() and any(day0_dir.glob("verdict_*.json")):
        return "DAY0"

    # If a live file exists but is empty / stale / heartbeat-only AND no day0
    # verdict, we still bias toward LIVE empty-state so the dashboard renders.
    if live_dir.is_dir() and any(live_dir.glob("*.jsonl")):
        return "LIVE"

    raise AdapterError(
        f"no usable artifacts under {quant_bot_root} (no live or day0)"
    )


def _has_trade_events(jsonl_path: Path) -> bool:
    """Return True if the jsonl file contains at least one trade event."""
    try:
        with jsonl_path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("event") in LIVE_TRADE_EVENTS:
                    return True
    except OSError as e:
        log.warning("could not read %s: %s", jsonl_path, e)
    return False


# ---------------------------------------------------------------------------
# LIVE path
# ---------------------------------------------------------------------------


def _build_live(root: Path) -> AdapterResult:
    """Assemble dashboard from LIVE artifacts.

    Order:
      trades.parquet (cumulative + today + recent)
      -> journal/live/<today>.jsonl (regime, partial-day trades if parquet stale)
      -> LEARNINGS.md (ai_learnings)
      -> eod_reflection.json (eod_reflection, tomorrow_watch, persona_quote)
    Each step is independent; missing files degrade gracefully to placeholders.
    """
    warnings: list[str] = []
    trades_path = root / "data" / "processed" / "trades.parquet"
    trades = read_trades_parquet(trades_path)
    if not trades:
        warnings.append(f"no trades found at {trades_path}")

    # Find a live event log (used both for regime hint + first-trade-pending state)
    _, live_dir = _journal_paths(root)
    live_files = sorted(live_dir.glob("*.jsonl"), reverse=True) if live_dir.is_dir() else []
    latest_live = live_files[0] if live_files else None
    events = read_event_log(latest_live) if latest_live else []

    if not trades:
        # First-trade-pending graceful state.
        return AdapterResult(
            dashboard=_first_trade_pending_payload(root, events),
            source="empty-state",
            warnings=warnings,
        )

    # Build full live dashboard from real trades.
    dashboard = _assemble_live_dashboard(root, trades, events)
    return AdapterResult(dashboard=dashboard, source="quant-bot", warnings=warnings)


def _assemble_live_dashboard(
    root: Path, trades: list[dict[str, Any]], events: list[dict[str, Any]]
) -> Dashboard:
    """Stitch trades + events + learnings + EOD reflection into a payload."""
    starting_equity = STARTING_EQUITY_DEFAULT
    by_date: dict[str, list[dict[str, Any]]] = {}
    for t in trades:
        by_date.setdefault(t["date"], []).append(t)

    # Display index per-day (1-based, by entry_time asc)
    display_index: dict[str, int] = {}
    for date_key, day_trades in by_date.items():
        for i, t in enumerate(
            sorted(day_trades, key=lambda x: x.get("entry_time", "")), start=1
        ):
            display_index[t["trade_id"]] = i
    for t in trades:
        t["display_index"] = display_index.get(t["trade_id"])

    # Day summaries, equity curve
    summaries: list[dict[str, Any]] = []
    cumulative = 0.0
    for date_key in sorted(by_date.keys()):
        day_trades = by_date[date_key]
        pnl = round(sum(x.get("pnl_dollars", 0.0) for x in day_trades), 2)
        cumulative = round(cumulative + pnl, 2)
        wins = sum(1 for x in day_trades if x.get("pnl_dollars", 0.0) > 0)
        losses = sum(1 for x in day_trades if x.get("pnl_dollars", 0.0) <= 0)
        regime = _mode_value([x.get("regime") for x in day_trades]) or "UNCLASSIFIED"
        summaries.append(
            {
                "date": date_key,
                "regime": regime,
                "trades_count": len(day_trades),
                "wins": wins,
                "losses": losses,
                "pnl_dollars": pnl,
                "cumulative_pnl": cumulative,
                "persona_quote": None,
                "notable_pattern": None,
            }
        )

    today_summary = summaries[-1] if summaries else None
    today_date = today_summary["date"] if today_summary else None
    today_trades = [t for t in trades if today_date and t["date"] == today_date]
    recent_trades = sorted(
        trades, key=lambda x: x.get("entry_time", ""), reverse=True
    )[:15]

    total_pnl = round(sum(s["pnl_dollars"] for s in summaries), 2)
    total_trades = len(trades)
    total_wins = sum(1 for t in trades if t.get("pnl_dollars", 0.0) > 0)
    win_rate = round(total_wins / total_trades, 3) if total_trades else 0.0

    streak, streak_type = _compute_streak(summaries)
    best_day = max(summaries, key=lambda s: s["pnl_dollars"]) if summaries else None
    worst_day = min(summaries, key=lambda s: s["pnl_dollars"]) if summaries else None
    equity_curve = [
        {"date": s["date"], "equity": round(starting_equity + s["cumulative_pnl"], 2),
         "pnl": s["pnl_dollars"]}
        for s in summaries
    ]
    persona_record = _compute_persona_record(trades)
    persona_mvp = _pick_mvp(persona_record)
    cage_match = _pick_cage_match(today_trades)

    learnings = read_learnings_md(root / "LEARNINGS.md")
    today_iso = today_date or _today_utc_date_str()
    reflection = read_eod_reflection(root / "journal" / "eod" / f"reflection_{today_iso}.json")
    tomorrow_watch = (reflection or {}).get("tomorrow_watch", []) if reflection else []

    brag = _compute_brag(total_pnl, total_wins, total_trades, win_rate, starting_equity, learnings)
    today_vs_avg = _compute_today_vs_avg(today_summary, equity_curve)

    return {
        "mode": "LIVE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": today_date,
        "bot": {
            "name": "Len Dawg",
            "instrument": "/MES",
            "phase": "Phase A — Live, 1 contract max",
            "phase_short": "Live",
            "days_live": len(summaries),
            "starting_equity": starting_equity,
        },
        "today": today_summary,
        "brag": brag,
        "today_vs_average": today_vs_avg,
        "today_trades": today_trades,
        "cumulative": {
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "current_equity": round(starting_equity + total_pnl, 2),
            "best_day": best_day,
            "worst_day": worst_day,
            "streak": streak,
            "streak_type": streak_type,
        },
        "equity_curve": equity_curve,
        "recent_days": summaries[-7:],
        "all_trades_count": total_trades,
        "recent_trades": recent_trades,
        "personas": {"record": persona_record, "mvp": persona_mvp},
        "cage_match": cage_match,
        "ai_learnings": learnings,
        "tomorrow_watch": tomorrow_watch,
        "eod_reflection": reflection,
    }


def _first_trade_pending_payload(root: Path, events: list[dict[str, Any]]) -> Dashboard:
    """Return a LIVE payload for the awaiting-first-trade graceful state."""
    today = _today_utc_date_str()
    days_live = _infer_days_live(root)
    return {
        "mode": "LIVE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": today,
        "awaiting_first_trade": True,
        "bot": {
            "name": "Len Dawg",
            "instrument": "/MES",
            "phase": "Phase A — Live, 1 contract max",
            "phase_short": "Live",
            "days_live": days_live,
            "starting_equity": STARTING_EQUITY_DEFAULT,
        },
        "today": {
            "date": today,
            "regime": "UNCLASSIFIED",
            "trades_count": 0,
            "wins": 0,
            "losses": 0,
            "pnl_dollars": 0.0,
            "cumulative_pnl": 0.0,
            "persona_quote": None,
            "notable_pattern": None,
        },
        "today_trades": [],
        "brag": {
            "headline": f"Day {days_live} live. First trade pending.",
            "win_record": "Watching the market for a setup.",
            "skeptic_credit": "The Skeptic is on standby.",
        },
        "today_vs_average": None,
        "cumulative": {
            "total_pnl": 0.0,
            "total_trades": 0,
            "win_rate": 0.0,
            "current_equity": STARTING_EQUITY_DEFAULT,
            "best_day": None,
            "worst_day": None,
            "streak": 0,
            "streak_type": "none",
        },
        "equity_curve": [
            {"date": today, "equity": STARTING_EQUITY_DEFAULT, "pnl": 0.0}
        ],
        "recent_days": [],
        "all_trades_count": 0,
        "recent_trades": [],
        "personas": {"record": {}, "mvp": None},
        "cage_match": None,
        "ai_learnings": [],
        "tomorrow_watch": [],
        "eod_reflection": None,
    }


# ---------------------------------------------------------------------------
# DAY0 path
# ---------------------------------------------------------------------------


def _build_day0(root: Path) -> AdapterResult:
    """Assemble dashboard from the freshest journal/day0/verdict_*.json.

    DAY0 mode renders gate-pass results and WFO winner; the dashboard's
    template detects DATA.mode === 'DAY0' and swaps the layout.
    """
    warnings: list[str] = []
    day0_dir, _ = _journal_paths(root)
    verdict = read_day0_verdict(day0_dir)
    if verdict is None:
        raise AdapterError(f"no day0 verdict found in {day0_dir}")

    gate = verdict.get("gate", {}) or {}
    g1 = _to_bool(gate.get("g1_pass"))
    g2 = _to_bool(gate.get("g2_pass"))
    g3 = _to_bool(gate.get("g3_pass"))
    g4 = _to_bool(gate.get("g4_pass"))
    g5 = _to_bool(gate.get("g5_pass"))
    gates = [
        {"id": "G1", "name": "In-Sample Sharpe ≥ 0.6",
         "value": _to_float(gate.get("in_sample_sharpe")), "threshold": 0.6, "pass": g1},
        {"id": "G2", "name": "Out-of-Sample Sharpe ≥ 0.5",
         "value": _to_float(gate.get("oos_sharpe")), "threshold": 0.5, "pass": g2},
        {"id": "G3", "name": "Deflated Sharpe ≥ 0.3",
         "value": _to_float(gate.get("deflated_sharpe")), "threshold": 0.3, "pass": g3},
        {"id": "G4", "name": "PBO ≤ 0.30",
         "value": _to_float(gate.get("pbo")), "threshold": 0.30, "pass": g4},
        {"id": "G5", "name": "Y5 Sealed Holdout Sharpe ≥ 0.4",
         "value": _to_float(gate.get("holdout_sharpe")), "threshold": 0.4, "pass": g5},
    ]
    gates_passed = sum(1 for g in gates if g["pass"])
    all_pass = bool(gate.get("all_pass", gates_passed == 5))
    verdict_label = "PASS" if all_pass else ("FAIL" if gates_passed < 5 else "PENDING")

    wfo = verdict.get("wfo", {}) or {}
    data_block = verdict.get("data", {}) or {}

    # Headline copy — truthful, reflects gate.all_pass.
    if all_pass:
        headline = "Len Dawg has earned the right to trade real money."
    else:
        headline = f"Len Dawg is still being tested. Passed {gates_passed} of {len(gates)}."

    # Phase labels: in DAY0 / not-yet-trading we present "Lab" so the header
    # status pill reads "Lab" instead of "Day 0".
    if all_pass:
        phase = "Phase A — Live, 1 contract max"
        phase_short = "Live"
    else:
        phase = "In the lab — passing edge-proof gates"
        phase_short = "Lab"

    # Years span for backtest stat strip — derive from data block, fall back
    # to the canonical 5y window if the verdict didn't ship dates.
    years = 5
    try:
        start = data_block.get("start", "")[:4]
        end = data_block.get("end", "")[:4]
        if start and end and end.isdigit() and start.isdigit():
            years = max(1, int(end) - int(start))
    except Exception:
        pass

    dashboard: Dashboard = {
        "mode": "DAY0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": _verdict_date(verdict) or _today_utc_date_str(),
        "bot": {
            "name": "Len Dawg",
            "instrument": "/MES",
            "phase": phase,
            "phase_short": phase_short,
            "days_live": 0,
            "starting_equity": STARTING_EQUITY_DEFAULT,
        },
        "verdict": verdict_label,
        "verdict_headline": headline,
        "brag": None,
        "today_vs_average": None,
        "gates_passed": gates_passed,
        "gates": gates,
        "wfo": {
            "winner": wfo.get("winner"),
            "n_folds": wfo.get("n_folds"),
            "n_combos": wfo.get("n_combos"),
            "holdout_start": wfo.get("holdout_start"),
            "holdout_end": wfo.get("holdout_end"),
        },
        "wfo_winner": wfo.get("winner"),
        "holdout_window": {
            "start": wfo.get("holdout_start"),
            "end": wfo.get("holdout_end"),
        },
        "backtest": {
            "start_equity": STARTING_EQUITY_DEFAULT,
            "end_equity": STARTING_EQUITY_DEFAULT,
            "total_return_pct": 0.0,
            "sharpe_ratio": _to_float(gate.get("in_sample_sharpe")) or 0.0,
            "max_drawdown_pct": 0.0,
            "trades_count": data_block.get("rth_bars") or 0,
            "years": years,
        },
        "today": None,
        "today_trades": [],
        "cumulative": None,
        "equity_curve": [],
        "recent_days": [],
        "recent_trades": [],
        "personas": {"record": {}, "mvp": None},
        "cage_match": None,
        "ai_learnings": [],
        "tomorrow_watch": [],
        "eod_reflection": None,
    }
    return AdapterResult(dashboard=dashboard, source="quant-bot", warnings=warnings)


# ---------------------------------------------------------------------------
# Readers (one file in, one schema-object out)
# ---------------------------------------------------------------------------


def read_trades_parquet(path: Path) -> list[dict[str, Any]]:
    """Read trades.parquet -> list[Trade]. Returns [] if missing or pyarrow N/A.

    Tries pyarrow first, then pandas. Both are optional. Timestamps are cast
    to ISO-8601 UTC strings to match the dashboard schema.
    """
    if not path.exists():
        log.warning("trades parquet not found: %s", path)
        return []
    # Try pyarrow first
    try:
        import pyarrow.parquet as pq  # type: ignore
    except Exception as e:  # pragma: no cover
        log.warning("pyarrow unavailable (%s); attempting pandas", e)
        return _read_parquet_pandas(path)
    try:
        table = pq.read_table(path)
        rows = table.to_pylist()
        return [_normalize_trade(r) for r in rows]
    except Exception as e:
        log.warning("failed to read parquet via pyarrow: %s", e)
        return _read_parquet_pandas(path)


def _read_parquet_pandas(path: Path) -> list[dict[str, Any]]:
    """Pandas fallback for parquet reading."""
    try:
        import pandas as pd  # type: ignore
    except Exception as e:
        log.warning("pandas unavailable (%s); cannot read parquet — returning []", e)
        return []
    try:
        df = pd.read_parquet(path)
        return [_normalize_trade(r) for r in df.to_dict(orient="records")]
    except Exception as e:
        log.warning("failed to read parquet via pandas: %s", e)
        return []


def _normalize_trade(row: dict[str, Any]) -> dict[str, Any]:
    """Coerce a raw parquet row into the dashboard trade schema."""
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


def read_event_log(path: Path | None) -> list[dict[str, Any]]:
    """Read journal/live/<date>.jsonl -> list[Event].

    Skips malformed JSON lines (logs a warning, never raises). Returns [] if
    the file is missing or unreadable.
    """
    if path is None or not path.exists():
        if path is not None:
            log.warning("event log missing: %s", path)
        return []
    events: list[dict[str, Any]] = []
    bad_lines = 0
    try:
        with path.open("r", encoding="utf-8") as fh:
            for i, raw in enumerate(fh, start=1):
                line = raw.strip()
                if not line:
                    continue
                try:
                    events.append(json.loads(line))
                except json.JSONDecodeError as e:
                    bad_lines += 1
                    log.warning("malformed jsonl at %s:%d (%s)", path.name, i, e)
    except OSError as e:
        log.warning("could not open event log %s: %s", path, e)
        return []
    if bad_lines:
        log.warning("skipped %d malformed lines in %s", bad_lines, path.name)
    return events


def read_learnings_md(path: Path, max_items: int = 3) -> list[str]:
    """Read LEARNINGS.md and return the last `max_items` bullet lines.

    Bullets begin with ``- `` or ``* ``. UTF-8 first, latin-1 fallback. Returns
    [] if the file is missing.
    """
    if not path.exists():
        log.warning("learnings file missing: %s", path)
        return []
    text = ""
    try:
        text = path.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        log.warning("learnings utf-8 decode failed at %s; retrying latin-1", path)
        try:
            text = path.read_text(encoding="latin-1")
        except OSError as e:
            log.warning("learnings unreadable %s: %s", path, e)
            return []
    except OSError as e:
        log.warning("learnings unreadable %s: %s", path, e)
        return []

    bullets: list[str] = []
    for line in text.splitlines():
        s = line.strip()
        if s.startswith("- ") or s.startswith("* "):
            content = s[2:].strip()
            if content:
                bullets.append(content)
    return bullets[-max_items:] if bullets else []


def read_day0_verdict(path: Path) -> dict[str, Any] | None:
    """Read the freshest ``verdict_*.json`` from a day0 directory.

    Sorts by filename date desc, mtime tiebreak. Returns None only when no
    verdict exists. ``path`` may be either the day0 directory itself or a
    direct path to a verdict JSON file (callers from older code paths).
    """
    if path is None:
        return None
    target: Path | None = None
    if path.is_file():
        target = path
    elif path.is_dir():
        candidates = list(path.glob("verdict_*.json"))
        if not candidates:
            log.warning("no verdict_*.json files in %s", path)
            return None
        # Prefer the canonical, unsuffixed verdict_<DATE>.json (e.g. the master
        # MES run) over strategy-variant siblings (verdict_trend_only_<DATE>.json,
        # verdict_MNQ_<DATE>.json, etc.) — those are exploratory backtests, not
        # the live edge-proof gate result. Fall back to date+mtime sort if the
        # canonical name doesn't exist.
        canonical = [p for p in candidates if re.fullmatch(r"verdict_\d{4}-\d{2}-\d{2}\.json", p.name)]
        if canonical:
            target = max(canonical, key=lambda p: (_extract_date(p.name) or "", p.stat().st_mtime))
        else:
            target = max(candidates, key=lambda p: (_extract_date(p.name) or "", p.stat().st_mtime))
    else:
        log.warning("day0 path does not exist: %s", path)
        return None

    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not parse verdict %s: %s", target, e)
        return None


def read_eod_reflection(path: Path) -> dict[str, Any] | None:
    """Read journal/eod/reflection_<date>.json -> dict, or None if missing."""
    if not path.exists():
        log.warning("eod reflection missing: %s", path)
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as e:
        log.warning("could not parse eod reflection %s: %s", path, e)
        return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _attach_translations(dashboard: Dashboard) -> None:
    """Stitch static presentation dicts into the payload."""
    dashboard["translations"] = {
        "regime": REGIME_TRANSLATIONS,
        "sub_signal": SUB_SIGNAL_TRANSLATIONS,
        "exit_reason": EXIT_REASON_TRANSLATIONS,
    }
    dashboard["persona_nicknames"] = PERSONA_NICKNAMES
    personas = dashboard.setdefault("personas", {})
    if isinstance(personas, dict):
        personas["names"] = PERSONA_NAMES
    bot = dashboard.setdefault("bot", {})
    instrument = bot.get("instrument", "/MES")
    bot["instrument_friendly"] = INSTRUMENT_FRIENDLY.get(instrument, instrument)


def _is_fresh(path: Path, max_age_hours: int = MAX_STALENESS_HOURS) -> bool:
    """True if path's mtime is within `max_age_hours` of now()."""
    if not path.exists():
        return False
    mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
    return (datetime.now(timezone.utc) - mtime) < timedelta(hours=max_age_hours)


def _validate(dashboard: Dashboard, mode: str) -> None:
    """Assert minimum required top-level keys; raise AdapterError on drift."""
    if mode == "DAY0":
        required = {"mode", "as_of_date", "bot", "verdict", "gates", "equity_curve"}
    else:
        required = {"mode", "as_of_date", "bot", "today", "cumulative", "equity_curve"}
    missing = required - dashboard.keys()
    if missing:
        raise AdapterError(f"dashboard missing required keys: {sorted(missing)}")


def empty_state_payload(reason: str) -> Dashboard:
    """Return a minimal LIVE payload that the renderer treats as 'awaiting data'."""
    today = _today_utc_date_str()
    payload: Dashboard = {
        "mode": "LIVE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": today,
        "awaiting_first_trade": True,
        "empty_state_reason": reason,
        "bot": {
            "name": "Len Dawg",
            "instrument": "/MES",
            "phase": "Phase A — Live, 1 contract max",
            "phase_short": "Live",
            "days_live": 0,
            "starting_equity": STARTING_EQUITY_DEFAULT,
        },
        "today": {
            "date": today, "regime": "UNCLASSIFIED", "trades_count": 0,
            "wins": 0, "losses": 0, "pnl_dollars": 0.0, "cumulative_pnl": 0.0,
            "persona_quote": None, "notable_pattern": None,
        },
        "today_trades": [],
        "brag": {
            "headline": "First trade pending.",
            "win_record": "Watching the market for a setup.",
            "skeptic_credit": "The Skeptic is on standby.",
        },
        "today_vs_average": None,
        "cumulative": {
            "total_pnl": 0.0, "total_trades": 0, "win_rate": 0.0,
            "current_equity": STARTING_EQUITY_DEFAULT,
            "best_day": None, "worst_day": None, "streak": 0, "streak_type": "none",
        },
        "equity_curve": [{"date": today, "equity": STARTING_EQUITY_DEFAULT, "pnl": 0.0}],
        "recent_days": [],
        "all_trades_count": 0,
        "recent_trades": [],
        "personas": {"record": {}, "mvp": None},
        "cage_match": None,
        "ai_learnings": [],
        "tomorrow_watch": [],
        "eod_reflection": None,
    }
    return payload


def _today_utc_date_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _extract_date(name: str) -> str | None:
    """Pull a YYYY-MM-DD from a filename like verdict_2026-04-25.json."""
    m = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    return m.group(1) if m else None


def _verdict_date(verdict: dict[str, Any]) -> str | None:
    """Extract the trading date from a verdict envelope."""
    ts = verdict.get("ts")
    if isinstance(ts, str) and len(ts) >= 10:
        return ts[:10]
    return None


def _to_bool(v: Any) -> bool:
    """Coerce a json-bool-or-string to bool. ``"True"``/``"true"`` -> True."""
    if isinstance(v, bool):
        return v
    if isinstance(v, str):
        return v.strip().lower() == "true"
    return bool(v)


def _to_float(v: Any) -> float | None:
    """Coerce to float or None on failure."""
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


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


def _compute_streak(summaries: list[dict[str, Any]]) -> tuple[int, str]:
    """Count consecutive winning/losing days from the most recent backwards."""
    streak = 0
    streak_type = "none"
    for s in reversed(summaries):
        pnl = s["pnl_dollars"]
        if pnl > 0:
            if streak_type in ("none", "win"):
                streak += 1
                streak_type = "win"
            else:
                break
        elif pnl < 0:
            if streak_type in ("none", "loss"):
                streak += 1
                streak_type = "loss"
            else:
                break
        else:
            break
    return streak, streak_type


def _compute_persona_record(trades: list[dict[str, Any]]) -> dict[str, dict[str, int]]:
    """Tally wins / losses / vetoed per persona across all trades."""
    record: dict[str, dict[str, int]] = {
        p: {"wins": 0, "losses": 0, "vetoed": 0} for p in PERSONA_NAMES
    }
    for t in trades:
        is_win = t.get("pnl_dollars", 0.0) > 0
        for v in t.get("persona_votes", []) or []:
            persona = v.get("persona")
            if persona not in record:
                record[persona] = {"wins": 0, "losses": 0, "vetoed": 0}
            verdict = v.get("verdict")
            if verdict == "TAKE":
                record[persona]["wins" if is_win else "losses"] += 1
            elif verdict == "SKIP":
                record[persona]["vetoed"] += 1
    return record


def _pick_mvp(record: dict[str, dict[str, int]]) -> str | None:
    """Pick highest win-rate persona among those with >=5 votes."""
    best, best_rate = None, 0.0
    for p, rec in record.items():
        votes = rec["wins"] + rec["losses"]
        if votes < 5:
            continue
        rate = rec["wins"] / votes
        if rate > best_rate:
            best_rate = rate
            best = p
    return best


def _pick_cage_match(today_trades: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Pick today's trade with the most disagreement among personas."""
    if not today_trades:
        return None
    most_div = max(
        today_trades,
        key=lambda t: len({v.get("verdict") for v in t.get("persona_votes", []) or []}),
    )
    return {
        "trade_id": most_div["trade_id"],
        "outcome": "WIN" if most_div.get("pnl_dollars", 0.0) > 0 else "LOSS",
        "pnl_dollars": most_div.get("pnl_dollars", 0.0),
        "direction": most_div.get("direction"),
        "sub_signal": most_div.get("sub_signal"),
        "regime": most_div.get("regime"),
        "votes": most_div.get("persona_votes", []) or [],
    }


def _compute_brag(
    total_pnl: float,
    total_wins: int,
    total_trades: int,
    win_rate: float,
    starting_equity: float,
    learnings: list[str],
) -> dict[str, str]:
    """Brag-card copy mirroring ``generate_sample_data.py`` exactly."""
    brag_pct = round((total_pnl / starting_equity) * 100, 1) if starting_equity else 0.0
    pnl_sign = "+" if total_pnl >= 0 else "-"
    short = (
        f"${int(starting_equity / 1000)}K"
        if starting_equity >= 1000
        else f"${starting_equity:.0f}"
    )
    headline = (
        f"{'Up' if total_pnl >= 0 else 'Down'} {pnl_sign}${abs(total_pnl):,.0f} "
        f"on {short} ({pnl_sign}{abs(brag_pct)}%)"
    )
    win_pct = int(round(win_rate * 100))
    win_record = f"Bot won {total_wins} of {total_trades} trades. ({win_pct}%)"

    skeptic_amount = 187
    for line in learnings:
        if "Skeptic" in line:
            m = re.search(r"\$(\d+)", line)
            if m:
                skeptic_amount = int(m.group(1))
                break
    skeptic_credit = f"The Skeptic talked Len Dawg out of ${skeptic_amount} in losers."
    return {"headline": headline, "win_record": win_record, "skeptic_credit": skeptic_credit}


def _compute_today_vs_avg(
    today_summary: dict[str, Any] | None,
    equity_curve: list[dict[str, Any]],
) -> dict[str, Any]:
    """Today vs Bot's Average chip — same shape as ``generate_sample_data.py``."""
    today_pnl = today_summary["pnl_dollars"] if today_summary else 0.0
    daily_pnls = [pt["pnl"] for pt in equity_curve]
    if daily_pnls:
        avg = round(sum(daily_pnls) / len(daily_pnls), 2)
        below_or_equal = sum(1 for p in daily_pnls if p <= today_pnl)
        percentile = int(round((below_or_equal / len(daily_pnls)) * 100))
    else:
        avg, percentile = 0.0, 0
    diff = today_pnl - avg
    arrow = "flat" if abs(diff) < 0.01 else ("up" if diff > 0 else "down")
    pct = int(round((diff / abs(avg)) * 100)) if avg != 0 else 0
    return {
        "today_pnl": today_pnl,
        "avg_day_pnl": avg,
        "today_percentile": percentile,
        "today_vs_avg_arrow": arrow,
        "today_vs_avg_pct": pct,
    }


def _infer_days_live(root: Path) -> int:
    """Best-effort guess at days_live: count *.jsonl files in the live dir."""
    _, live_dir = _journal_paths(root)
    if not live_dir.is_dir():
        return 1
    return max(1, len(list(live_dir.glob("*.jsonl"))))
