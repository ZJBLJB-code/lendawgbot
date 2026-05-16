"""Generate deterministic sample data for TomCash dashboard.

Two output files:
- data/sample.json      → "Live" mode: 14 days of trades, persona votes, EOD reflections
- data/day0_sample.json → "Lab" mode: edge-proof gate verdict, backtest curve

Schema mirrors what quant-bot will produce per PLAN.md §8.1, §8.2, §6.1.

Run: python scripts/generate_sample_data.py
"""
from __future__ import annotations

import json
import random
import re
from dataclasses import asdict, dataclass, field
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Any

random.seed(101)

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
DATA.mkdir(exist_ok=True)

# -------- Tunables ---------------------------------------------------------

DAYS = 14
TODAY = datetime(2026, 4, 24, tzinfo=timezone.utc)
START_EQUITY = 10_000.00
TICK_VALUE = 1.25  # /MES: $1.25 per 0.25 point
SUB_SIGNALS = ["ORF", "VWAP_REV", "GAP_FILL"]
REGIMES = ["MEAN_REV_FRIENDLY", "TREND_REGIME", "VOLATILE", "UNCLASSIFIED"]
REGIME_WEIGHTS = [0.55, 0.20, 0.20, 0.05]

# -------- Persona library --------------------------------------------------

PERSONAS = ["TT", "ME", "OF", "DA"]

PERSONA_NAMES = {
    "TT": "Technical Trader",
    "ME": "Macro Economist",
    "OF": "Options Analyst",
    "DA": "Devil's Advocate",
}

PERSONA_NICKNAMES = {
    "TT": "The Chartist",
    "ME": "The Macro Guy",
    "OF": "The Flow Reader",
    "DA": "The Skeptic",
}

# Dry, witty one-liners — translated for laypeople. Devil's Advocate carries
# the comedy.
PERSONA_LINES = {
    "TT": [
        "The market kept knocking at the door but couldn't get in. Took the fade.",
        "The line held perfectly. I love it when charts behave.",
        "The gap closed by 10:15. Closed the trade.",
        "Strong trend day. I had to sit on my hands. Harder than it sounds.",
        "Volume disappeared right at our level. Took it.",
        "Sellers gave up. Buyers stepped right in.",
        "Played out exactly as the script said.",
    ],
    "ME": [
        "Inflation came in soft. Stocks loved it. The trade lined up.",
        "Dollar's slipping. That's a tailwind for stocks today.",
        "Fed meeting tomorrow. I said cut size. We didn't. We got lucky.",
        "Bonds and stocks both rallying. Clean signal.",
        "No big news today. Quiet days are the bot's friend.",
        "Jobs report missed. Nobody cared. Market's looking past it.",
        "Watching the yen — could get bumpy if Japan moves.",
    ],
    "OF": [
        "The options market is pulling the index toward 5380 like a magnet.",
        "Big bets on the downside are holding the line. Floor's solid.",
        "Dealers are pinning the price near 5400. Sit in the range.",
        "No fear in the options market. Quiet-day setup.",
        "Mechanical flows favor up into the close. Held the bid.",
        "After 3pm the tape gets sticky. Stay patient.",
        "Wall of resistance above. Don't expect a breakout today.",
    ],
    "DA": [
        "Everyone's bullish. That's usually the whole problem.",
        "I'd skip this. I will be wrong, and I will be smug about it later.",
        "Three winners in a row. Statistically, I am due to be vindicated.",
        "The thesis is fine. It's also what everyone else is doing.",
        "Take the trade. But promise me you will not add to it.",
        "If this fails, fail fast. Don't argue with the tape.",
        "The setup is clean. I am suspicious of clean setups.",
        "You'll thank me later. Or not. Mostly not.",
        "Bull case is loud. Bear case is quiet. Quiet usually wins.",
    ],
}

# What the AI "learned" — short, dry, post-hoc reflections
LEARNINGS = [
    "Opening fades work better on big overnight moves. Tiny moves are just noise.",
    "On inflation-report mornings, the bot stays out until 11am ET. Lessons learned.",
    "The Skeptic's vetoes saved $187 over the last 30 trades. The grump earns his keep.",
    "Most stop-outs happen between 1:30 and 2:00 PM ET. The bot now sits that out.",
    "Bigger gaps fill more often. Below half a percent it's a coin flip.",
    "The Macro Guy's confidence calibrates well. Trust it.",
    "The Flow Reader gets noisy on quiet days. The bot down-weights him then.",
    "The fourth trade of the day is usually the worst. Cap at three.",
    "Mondays after a quiet weekend are the bot's best day of the week.",
]

PATTERNS = [
    "Three winning opening-fade setups in a row when overnight ranges were big.",
    "Two losses in a row on choppy days — bot may be mis-reading the regime.",
    "The Skeptic vetoed two setups today; one would have lost, one would have won.",
    "The Chartist and The Skeptic disagreed on 30% of trades this week, up from 12%.",
    "Win rate this week is running hot — 64% vs. the 55% the bot expects long-term.",
]

# -------- Data classes -----------------------------------------------------


@dataclass
class PersonaVote:
    persona: str
    verdict: str  # TAKE | SKIP | REDUCE_SIZE
    confidence: float  # 0..1
    reasoning: str
    size_multiplier: float = 1.0


@dataclass
class Trade:
    trade_id: str
    date: str
    entry_time: str
    exit_time: str
    direction: str  # LONG | SHORT
    sub_signal: str
    regime: str
    qty: int
    entry_price: float
    exit_price: float
    stop_price: float
    target_price: float
    pnl_points: float
    pnl_dollars: float
    bars_held: int
    exit_reason: str  # TARGET | STOP | TIME | TRAILING
    persona_votes: list[PersonaVote]
    thesis_confirmed: bool


@dataclass
class DaySummary:
    date: str
    regime: str
    trades_count: int
    wins: int
    losses: int
    pnl_dollars: float
    cumulative_pnl: float
    persona_quote: dict[str, str]  # {"persona": "DA", "line": "..."}
    notable_pattern: str | None = None


@dataclass
class EodReflection:
    date: str
    wins_summary: list[str]
    losses_summary: list[str]
    persona_accuracy: dict[str, float]
    patterns_observed: list[str]
    tomorrow_watch: list[str]


# -------- Generation -------------------------------------------------------


def round_tick(price: float) -> float:
    """Round to nearest /MES tick (0.25)."""
    return round(price * 4) / 4


def gen_trade(date: datetime, idx: int, regime: str, cumulative: float) -> Trade:
    """Generate one realistic trade with persona votes."""
    sub_signal = random.choices(SUB_SIGNALS, weights=[0.5, 0.3, 0.2])[0]
    direction = random.choice(["LONG", "SHORT"])
    qty = 1

    # Entry/exit times within RTH (9:30 ET = 13:30 UTC, 16:00 ET = 20:00 UTC)
    entry_minutes = random.randint(0, 360)
    entry_dt = date.replace(hour=13, minute=30) + timedelta(minutes=entry_minutes)
    held_bars = random.randint(3, 30)
    exit_dt = entry_dt + timedelta(minutes=held_bars * 5)
    if exit_dt.hour >= 20:
        exit_dt = entry_dt.replace(hour=19, minute=55)

    # Price model: 5400 base + drift
    entry_price = round_tick(5400 + random.uniform(-30, 30))

    # Win probability shifted by regime
    win_p = {"MEAN_REV_FRIENDLY": 0.62, "TREND_REGIME": 0.45,
             "VOLATILE": 0.40, "UNCLASSIFIED": 0.50}[regime]
    is_winner = random.random() < win_p

    # Reward:risk ~1.6 winners, ~1.0 losers (slight slippage on losses)
    if is_winner:
        move_points = random.uniform(2.0, 8.0) * (1 if direction == "LONG" else -1)
        exit_reason = "TARGET"
    else:
        move_points = random.uniform(-5.0, -1.5) * (1 if direction == "LONG" else -1)
        exit_reason = random.choice(["STOP", "TIME"])

    exit_price = round_tick(entry_price + move_points)
    pnl_points = (exit_price - entry_price) * (1 if direction == "LONG" else -1)
    pnl_dollars = pnl_points * 4 * TICK_VALUE * qty - 5.0  # commission/slippage
    pnl_dollars = round(pnl_dollars, 2)

    stop_offset = random.uniform(2.5, 4.0)
    target_offset = random.uniform(4.0, 7.0)
    if direction == "LONG":
        stop_price = round_tick(entry_price - stop_offset)
        target_price = round_tick(entry_price + target_offset)
    else:
        stop_price = round_tick(entry_price + stop_offset)
        target_price = round_tick(entry_price - target_offset)

    # Persona votes: aligned-ish on TAKE for trades that happened (we filter
    # SKIPs out — bot only takes if 3/4+ TAKE)
    votes: list[PersonaVote] = []
    for p in PERSONAS:
        # DA more likely to skip; others mostly take
        if p == "DA":
            verdict = random.choices(["TAKE", "REDUCE_SIZE", "SKIP"],
                                     weights=[0.4, 0.4, 0.2])[0]
        else:
            verdict = random.choices(["TAKE", "REDUCE_SIZE", "SKIP"],
                                     weights=[0.7, 0.2, 0.1])[0]
        confidence = round(random.uniform(0.5, 0.92), 2)
        reasoning = random.choice(PERSONA_LINES[p])
        size_mult = {"TAKE": 1.0, "REDUCE_SIZE": 0.5, "SKIP": 0.0}[verdict]
        votes.append(PersonaVote(p, verdict, confidence, reasoning, size_mult))

    # Force at least one TAKE so the trade makes sense
    if not any(v.verdict == "TAKE" for v in votes):
        votes[0].verdict = "TAKE"
        votes[0].size_multiplier = 1.0

    thesis_confirmed = is_winner if random.random() > 0.15 else not is_winner

    return Trade(
        trade_id=f"T-{date.strftime('%Y%m%d')}-{idx:02d}",
        date=date.strftime("%Y-%m-%d"),
        entry_time=entry_dt.isoformat(),
        exit_time=exit_dt.isoformat(),
        direction=direction,
        sub_signal=sub_signal,
        regime=regime,
        qty=qty,
        entry_price=entry_price,
        exit_price=exit_price,
        stop_price=stop_price,
        target_price=target_price,
        pnl_points=round(pnl_points, 2),
        pnl_dollars=pnl_dollars,
        bars_held=held_bars,
        exit_reason=exit_reason,
        persona_votes=votes,
        thesis_confirmed=thesis_confirmed,
    )


def gen_day(date: datetime, cumulative: float) -> tuple[list[Trade], DaySummary, EodReflection]:
    """Generate one trading day."""
    regime = random.choices(REGIMES, weights=REGIME_WEIGHTS)[0]
    trades_count = random.choices([0, 1, 2, 3, 4, 5], weights=[0.1, 0.15, 0.25, 0.25, 0.15, 0.1])[0]
    trades = [gen_trade(date, i, regime, cumulative) for i in range(trades_count)]

    pnl = round(sum(t.pnl_dollars for t in trades), 2)
    wins = sum(1 for t in trades if t.pnl_dollars > 0)
    losses = sum(1 for t in trades if t.pnl_dollars <= 0)
    new_cumulative = round(cumulative + pnl, 2)

    # Pick a quote for the day — favor DA on losing days, TT on winners
    if pnl < -50:
        quote_persona = "DA"
    elif pnl > 100:
        quote_persona = random.choice(["TT", "OF"])
    else:
        quote_persona = random.choice(PERSONAS)
    quote_line = random.choice(PERSONA_LINES[quote_persona])

    pattern = random.choice(PATTERNS) if random.random() < 0.4 else None

    summary = DaySummary(
        date=date.strftime("%Y-%m-%d"),
        regime=regime,
        trades_count=trades_count,
        wins=wins,
        losses=losses,
        pnl_dollars=pnl,
        cumulative_pnl=new_cumulative,
        persona_quote={"persona": quote_persona, "line": quote_line},
        notable_pattern=pattern,
    )

    # EOD reflection
    wins_summary = [
        f"{t.trade_id}: thesis confirmed, +${t.pnl_dollars:.0f}"
        for t in trades if t.pnl_dollars > 0
    ][:3]
    losses_summary = [
        f"{t.trade_id}: {t.exit_reason.lower()}, -${abs(t.pnl_dollars):.0f}"
        for t in trades if t.pnl_dollars <= 0
    ][:3]
    persona_accuracy = {
        p: round(random.uniform(0.45, 0.78), 2) for p in PERSONAS
    }
    patterns = random.sample(PATTERNS, k=min(2, len(PATTERNS)))
    tomorrow = [
        "FOMC minutes 14:00 ET — flat into release.",
        "Roll-over week for /MES. Watch volume.",
        "VIX < 14, expect tight ranges.",
        "Earnings-heavy week, watch single-name spillover.",
    ]
    tomorrow_watch = random.sample(tomorrow, k=2)

    reflection = EodReflection(
        date=date.strftime("%Y-%m-%d"),
        wins_summary=wins_summary,
        losses_summary=losses_summary,
        persona_accuracy=persona_accuracy,
        patterns_observed=patterns,
        tomorrow_watch=tomorrow_watch,
    )

    return trades, summary, reflection


def gen_live_mode() -> dict[str, Any]:
    """Generate the canonical live-mode dataset."""
    all_trades: list[Trade] = []
    summaries: list[DaySummary] = []
    reflections: list[EodReflection] = []

    cumulative = 0.0
    # Generate days going backward, then reverse
    days_data = []
    for offset in range(DAYS - 1, -1, -1):
        date = TODAY - timedelta(days=offset)
        # Skip weekends
        if date.weekday() >= 5:
            continue
        trades, summary, reflection = gen_day(date, cumulative)
        cumulative = summary.cumulative_pnl
        days_data.append((trades, summary, reflection))

    for trades, summary, reflection in days_data:
        all_trades.extend(trades)
        summaries.append(summary)
        reflections.append(reflection)

    today_summary = summaries[-1] if summaries else None
    today_trades = [t for t in all_trades if today_summary and t.date == today_summary.date]

    # Equity curve point per day
    equity_curve = [{"date": s.date, "equity": round(START_EQUITY + s.cumulative_pnl, 2),
                     "pnl": s.pnl_dollars} for s in summaries]

    # Cumulative metrics
    total_pnl = round(sum(s.pnl_dollars for s in summaries), 2)
    total_trades = len(all_trades)
    total_wins = sum(1 for t in all_trades if t.pnl_dollars > 0)
    win_rate = round(total_wins / total_trades, 3) if total_trades else 0.0

    # Streak: count consecutive winning or losing days from end
    streak = 0
    streak_type = "none"
    for s in reversed(summaries):
        if s.pnl_dollars > 0:
            if streak_type in ("none", "win"):
                streak += 1
                streak_type = "win"
            else:
                break
        elif s.pnl_dollars < 0:
            if streak_type in ("none", "loss"):
                streak += 1
                streak_type = "loss"
            else:
                break

    # Best & worst day
    best_day = max(summaries, key=lambda s: s.pnl_dollars)
    worst_day = min(summaries, key=lambda s: s.pnl_dollars)

    # Per-persona record this period
    persona_record: dict[str, dict[str, int]] = {p: {"wins": 0, "losses": 0, "vetoed": 0} for p in PERSONAS}
    for t in all_trades:
        is_win = t.pnl_dollars > 0
        for v in t.persona_votes:
            if v.verdict == "TAKE":
                if is_win:
                    persona_record[v.persona]["wins"] += 1
                else:
                    persona_record[v.persona]["losses"] += 1
            elif v.verdict == "SKIP":
                persona_record[v.persona]["vetoed"] += 1

    # MVP persona = highest win rate among those who voted >5 times
    persona_mvp = None
    best_rate = 0.0
    for p, rec in persona_record.items():
        votes = rec["wins"] + rec["losses"]
        if votes >= 5:
            rate = rec["wins"] / votes
            if rate > best_rate:
                best_rate = rate
                persona_mvp = p

    # Today's "AI Cage Match" — pull today's trade with most disagreement.
    # Inline direction & sub_signal so the renderer doesn't need to lookup-join.
    cage_match = None
    if today_trades:
        most_div = max(today_trades, key=lambda t: len({v.verdict for v in t.persona_votes}))
        cage_match = {
            "trade_id": most_div.trade_id,
            "outcome": "WIN" if most_div.pnl_dollars > 0 else "LOSS",
            "pnl_dollars": most_div.pnl_dollars,
            "direction": most_div.direction,
            "sub_signal": most_div.sub_signal,
            "regime": most_div.regime,
            "votes": [asdict(v) for v in most_div.persona_votes],
        }

    # Sort recent trades by entry_time descending (Engineer #32)
    recent_trades_sorted = sorted(
        all_trades, key=lambda t: t.entry_time, reverse=True
    )[:15]

    # display_index: 1-based per-day, computed by sorting that day's trades
    # by entry_time ascending. Add to today_trades and recent_trades dicts.
    display_index_by_id: dict[str, int] = {}
    trades_by_date: dict[str, list[Trade]] = {}
    for t in all_trades:
        trades_by_date.setdefault(t.date, []).append(t)
    for date_key, day_trades in trades_by_date.items():
        for i, t in enumerate(sorted(day_trades, key=lambda tr: tr.entry_time), start=1):
            display_index_by_id[t.trade_id] = i

    today_trades_dicts = []
    for t in today_trades:
        d = asdict(t)
        d["display_index"] = display_index_by_id.get(t.trade_id)
        today_trades_dicts.append(d)

    recent_trades_dicts = []
    for t in recent_trades_sorted:
        d = asdict(t)
        d["display_index"] = display_index_by_id.get(t.trade_id)
        recent_trades_dicts.append(d)

    # Brag card: single-line shareable summary
    brag_pct = round((total_pnl / START_EQUITY) * 100, 1) if START_EQUITY else 0.0
    pnl_sign = "+" if total_pnl >= 0 else "-"
    start_equity_short = f"${int(START_EQUITY / 1000)}K" if START_EQUITY >= 1000 else f"${START_EQUITY:.0f}"
    brag_headline = (
        f"{'Up' if total_pnl >= 0 else 'Down'} {pnl_sign}${abs(total_pnl):,.0f} "
        f"on {start_equity_short} ({pnl_sign}{abs(brag_pct)}%)"
    )
    win_rate_pct = int(round(win_rate * 100))
    brag_win_record = (
        f"Bot won {total_wins} of {total_trades} trades. ({win_rate_pct}%)"
    )

    # Skeptic credit: try to parse a $ amount from existing ai_learnings
    ai_learnings_list = locals().get("ai_learnings_sample") or []
    skeptic_amount = None
    skeptic_phrase = None
    # Search the LEARNINGS pool for a Skeptic-related line referencing dollars
    for line in LEARNINGS:
        if "Skeptic" in line:
            m = re.search(r"\$(\d+)", line)
            if m:
                skeptic_amount = int(m.group(1))
                skeptic_phrase = line
                break
    if skeptic_amount is None:
        # deterministic-from-seed fallback: pick from the persona_record vetoed counts
        skeptic_amount = 187
    skeptic_credit = (
        f"The Skeptic talked Len Dawg out of ${skeptic_amount} in losers."
    )

    brag = {
        "headline": brag_headline,
        "win_record": brag_win_record,
        "skeptic_credit": skeptic_credit,
    }

    # Today vs Bot's Average
    today_pnl_val = today_summary.pnl_dollars if today_summary else 0.0
    daily_pnls = [pt["pnl"] for pt in equity_curve]
    if daily_pnls:
        avg_day_pnl = round(sum(daily_pnls) / len(daily_pnls), 2)
    else:
        avg_day_pnl = 0.0
    # Percentile: 0-100 where today's pnl ranks vs all days in equity_curve
    if daily_pnls:
        below_or_equal = sum(1 for p in daily_pnls if p <= today_pnl_val)
        today_percentile = int(round((below_or_equal / len(daily_pnls)) * 100))
    else:
        today_percentile = 0
    diff = today_pnl_val - avg_day_pnl
    if abs(diff) < 0.01:
        arrow = "flat"
    elif diff > 0:
        arrow = "up"
    else:
        arrow = "down"
    if avg_day_pnl != 0:
        today_vs_avg_pct = int(round((diff / abs(avg_day_pnl)) * 100))
    else:
        today_vs_avg_pct = 0
    today_vs_average = {
        "today_pnl": today_pnl_val,
        "avg_day_pnl": avg_day_pnl,
        "today_percentile": today_percentile,
        "today_vs_avg_arrow": arrow,
        "today_vs_avg_pct": today_vs_avg_pct,
    }

    return {
        "mode": "LIVE",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "as_of_date": today_summary.date if today_summary else None,
        "bot": {
            "name": "Len Dawg",
            "instrument": "/MES",
            "instrument_friendly": "S&P 500 mini",
            "phase": "Phase A — Live, 1 contract max",
            "phase_short": "Live",
            "days_live": len(summaries),
            "starting_equity": START_EQUITY,
        },
        "translations": {
            "regime": {
                "MEAN_REV_FRIENDLY": "Calm, choppy market",
                "TREND_REGIME": "Strong trend day",
                "VOLATILE": "Choppy, wild market",
                "UNCLASSIFIED": "Unclear regime",
            },
            "sub_signal": {
                "ORF": "Opening fade",
                "VWAP_REV": "Pullback",
                "GAP_FILL": "Gap fill",
            },
            "exit_reason": {
                "TARGET": "hit goal",
                "STOP": "stopped out",
                "TIME": "ran out of time",
                "TRAILING": "trailed out",
            },
        },
        "persona_nicknames": PERSONA_NICKNAMES,
        "today": asdict(today_summary) if today_summary else None,
        "brag": brag,
        "today_vs_average": today_vs_average,
        "today_trades": today_trades_dicts,
        "cumulative": {
            "total_pnl": total_pnl,
            "total_trades": total_trades,
            "win_rate": win_rate,
            "current_equity": round(START_EQUITY + total_pnl, 2),
            "best_day": asdict(best_day),
            "worst_day": asdict(worst_day),
            "streak": streak,
            "streak_type": streak_type,
        },
        "equity_curve": equity_curve,
        "recent_days": [asdict(s) for s in summaries[-7:]],
        "all_trades_count": len(all_trades),
        "recent_trades": recent_trades_dicts,
        "personas": {
            "names": PERSONA_NAMES,
            "record": persona_record,
            "mvp": persona_mvp,
        },
        "cage_match": cage_match,
        "ai_learnings": random.sample(LEARNINGS, k=min(3, len(LEARNINGS))),
        "tomorrow_watch": reflections[-1].tomorrow_watch if reflections else [],
        "eod_reflection": asdict(reflections[-1]) if reflections else None,
    }


def gen_day0_mode() -> dict[str, Any]:
    """Generate Day 0 / lab mode: bot hasn't traded live yet, just edge-proof."""
    # 5-year synthetic backtest curve
    bt_days = 252 * 5
    curve = []
    eq = 10_000.0
    for i in range(bt_days):
        # Slight upward drift with noise
        daily_ret = random.gauss(0.0006, 0.011)
        eq *= 1 + daily_ret
        curve.append({"day": i, "equity": round(eq, 2)})

    final_eq = curve[-1]["equity"]
    sharpe = round(random.uniform(0.55, 0.75), 2)
    deflated_sharpe = round(sharpe - 0.18, 2)
    pbo = round(random.uniform(0.18, 0.32), 2)

    # Edge-proof gates G1-G5
    gates = [
        {"id": "G1", "name": "In-Sample Sharpe ≥ 0.6",
         "value": 0.84, "threshold": 0.6, "pass": True},
        {"id": "G2", "name": "Out-of-Sample Sharpe ≥ 0.5",
         "value": 0.62, "threshold": 0.5, "pass": True},
        {"id": "G3", "name": "Deflated Sharpe ≥ 0.3",
         "value": deflated_sharpe, "threshold": 0.3, "pass": deflated_sharpe >= 0.3},
        {"id": "G4", "name": "PBO ≤ 0.30",
         "value": pbo, "threshold": 0.30, "pass": pbo <= 0.30},
        {"id": "G5", "name": "Y5 Sealed Holdout Sharpe ≥ 0.4",
         "value": 0.51, "threshold": 0.4, "pass": True},
    ]
    gates_passed = sum(1 for g in gates if g["pass"])

    return {
        "mode": "DAY0",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "bot": {
            "name": "Len Dawg",
            "instrument": "/MES",
            "phase": "Day 0 — Edge-proof gate",
        },
        "verdict": "PASS" if gates_passed == 5 else "PENDING",
        "brag": None,
        "today_vs_average": None,
        "gates_passed": gates_passed,
        "gates": gates,
        "backtest": {
            "start_equity": 10_000,
            "end_equity": round(final_eq, 2),
            "total_return_pct": round((final_eq / 10_000 - 1) * 100, 1),
            "sharpe_ratio": sharpe,
            "max_drawdown_pct": round(random.uniform(8.0, 14.0), 1),
            "trades_count": random.randint(800, 1100),
            "years": 5,
        },
        "equity_curve": curve[::5],  # downsample to ~250 points
    }


def main() -> None:
    live = gen_live_mode()
    day0 = gen_day0_mode()
    (DATA / "sample.json").write_text(json.dumps(live, indent=2, default=str))
    (DATA / "day0_sample.json").write_text(json.dumps(day0, indent=2, default=str))
    print(f"Wrote {DATA / 'sample.json'} ({len(live.get('recent_trades', []))} recent trades)")
    print(f"Wrote {DATA / 'day0_sample.json'} ({day0['gates_passed']}/5 gates passed)")
    print(f"Today's P&L: ${live['today']['pnl_dollars']:.2f}" if live['today'] else "No today data")
    print(f"Cumulative: ${live['cumulative']['total_pnl']:.2f}, win rate {live['cumulative']['win_rate']*100:.1f}%")


if __name__ == "__main__":
    main()
