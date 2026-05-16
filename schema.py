"""TomCash dashboard schema — single source of truth for the quant-bot ↔ dashboard contract.

Frozen dataclasses with ``from_dict``/``to_dict``. Top-level envelope is
``Dashboard``. Versioned via ``Dashboard.schema_version`` (semver):
  MAJOR = breaking field change, MINOR = additive/optional, PATCH = docs.

Every nested type is documented inline. Optional fields default to ``None``
or a safe empty value so a partial payload still renders.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field, fields
from enum import Enum
from typing import Any, ClassVar


class Verdict(str, Enum):
    """Persona vote on a candidate trade (PLAN §6.1)."""
    TAKE = "TAKE"
    SKIP = "SKIP"
    REDUCE_SIZE = "REDUCE_SIZE"


def _strict(cls, d: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys (forward-compat with MINOR schema bumps)."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


@dataclass(frozen=True)
class Bot:
    name: str                                 # display name, e.g. "Len Dawg"
    instrument: str                           # exchange symbol, e.g. "/MES"
    phase_short: str                          # short label, e.g. "Live" / "Day 0"
    instrument_friendly: str | None = None    # plain-English name, e.g. "S&P 500 mini"
    phase: str | None = None                  # long phase string, e.g. "Phase A — 1 contract max"
    days_live: int | None = None              # trading days since go-live (LIVE mode only)
    starting_equity: float | None = None      # account equity at phase start

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Bot:
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class PersonaVote:
    persona: str                              # persona code: TT | ME | OF | DA
    verdict: Verdict                          # TAKE | SKIP | REDUCE_SIZE
    confidence: float                         # 0.0–1.0 self-reported confidence
    reasoning: str                            # one-line lay-friendly explanation
    size_multiplier: float = 1.0              # 0.0 (skip) | 0.5 (reduce) | 1.0 (take)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> PersonaVote:
        d = _strict(cls, dict(d))
        d["verdict"] = Verdict(d["verdict"])
        return cls(**d)


@dataclass(frozen=True)
class Persona:
    code: str                                 # short code, e.g. "TT"
    name: str                                 # full title, e.g. "Technical Trader"
    nickname: str                             # display name, e.g. "The Chartist"
    wins: int = 0                             # trades taken (voted TAKE) that won
    losses: int = 0                           # trades taken that lost
    vetoed: int = 0                           # trades persona voted SKIP on

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Persona:
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Trade:
    trade_id: str                             # stable id, e.g. "T-20260424-00"
    date: str                                 # ISO date, "YYYY-MM-DD"
    entry_time: str                           # ISO-8601 with tz
    exit_time: str                            # ISO-8601 with tz
    direction: str                            # "LONG" | "SHORT"
    sub_signal: str                           # "ORF" | "VWAP_REV" | "GAP_FILL"
    regime: str                               # regime classifier output
    qty: int                                  # contracts
    entry_price: float                        # filled entry
    exit_price: float                         # filled exit
    pnl_dollars: float                        # net P&L incl. commission/slippage
    pnl_points: float                         # gross points moved
    bars_held: int                            # 5-min bars between entry/exit
    exit_reason: str                          # TARGET | STOP | TIME | TRAILING
    persona_votes: list[PersonaVote]          # 3–4 persona votes, possibly ABSTAIN-missing
    stop_price: float | None = None           # planned stop, optional for replay logs
    target_price: float | None = None         # planned target
    thesis_confirmed: bool | None = None      # post-trade self-grade (PLAN §8.1)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Trade:
        d = _strict(cls, dict(d))
        d["persona_votes"] = [PersonaVote.from_dict(v) for v in d.get("persona_votes", [])]
        return cls(**d)


@dataclass(frozen=True)
class Day:
    date: str                                 # ISO date
    regime: str                               # regime label for the day
    trades_count: int                         # total trades taken
    wins: int                                 # winning trades
    losses: int                               # losing trades
    pnl_dollars: float                        # day net P&L
    cumulative_pnl: float                     # running total since phase start
    persona_quote: dict[str, str] | None = None   # {"persona": code, "line": str}
    notable_pattern: str | None = None        # one-line callout, optional

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Day:
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class EquityPoint:
    date: str                                 # ISO date
    equity: float                             # account equity at end-of-day
    pnl: float                                # day P&L

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EquityPoint:
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class CageMatch:
    trade_id: str                             # the most-disagreed-on trade today
    outcome: str                              # "WIN" | "LOSS"
    pnl_dollars: float                        # P&L of the trade
    direction: str                            # LONG | SHORT
    sub_signal: str                           # sub-signal label
    regime: str                               # regime label
    votes: list[PersonaVote]                  # full persona vote set

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> CageMatch:
        d = _strict(cls, dict(d))
        d["votes"] = [PersonaVote.from_dict(v) for v in d.get("votes", [])]
        return cls(**d)


@dataclass(frozen=True)
class EodReflection:
    date: str                                 # ISO date
    wins_summary: list[str] = field(default_factory=list)         # one-line per winner
    losses_summary: list[str] = field(default_factory=list)       # one-line per loser
    persona_accuracy: dict[str, float] = field(default_factory=dict)  # {code: 0..1}
    patterns_observed: list[str] = field(default_factory=list)    # patterns called out
    tomorrow_watch: list[str] = field(default_factory=list)       # what to watch tomorrow

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> EodReflection:
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Translations:
    """Code → lay-friendly string maps (renderer's i18n table)."""
    regime: dict[str, str] = field(default_factory=dict)          # e.g. {"VOLATILE": "Choppy, wild market"}
    sub_signal: dict[str, str] = field(default_factory=dict)      # e.g. {"ORF": "Opening fade"}
    exit_reason: dict[str, str] = field(default_factory=dict)     # e.g. {"STOP": "stopped out"}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Translations:
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Dashboard:
    """Top-level envelope. Renderer reads exactly this shape."""
    SCHEMA_VERSION: ClassVar[str] = "1.0.0"

    schema_version: str                       # semver, must match SCHEMA_VERSION major
    mode: str                                 # "LIVE" | "DAY0"
    generated_at: str                         # ISO-8601 UTC, when payload was built
    bot: Bot                                  # bot identity + phase
    translations: Translations                # code → lay-friendly maps
    equity_curve: list[EquityPoint]           # one point per trading day
    as_of_date: str | None = None             # most-recent trading date in payload
    today: Day | None = None                  # today's summary (LIVE)
    today_trades: list[Trade] = field(default_factory=list)        # today's trades
    cumulative: dict[str, Any] | None = None  # totals: total_pnl, win_rate, current_equity, streak…
    recent_days: list[Day] = field(default_factory=list)           # last ~7 days
    recent_trades: list[Trade] = field(default_factory=list)       # last ~15 trades, newest first
    personas: list[Persona] = field(default_factory=list)          # static + record per persona
    cage_match: CageMatch | None = None       # today's most-disagreed trade, optional
    eod_reflection: EodReflection | None = None                    # latest EOD reflection
    ai_learnings: list[str] = field(default_factory=list)          # 1–3 recent lessons
    tomorrow_watch: list[str] = field(default_factory=list)        # tomorrow callouts

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> Dashboard:
        d = dict(d)
        d.setdefault("schema_version", cls.SCHEMA_VERSION)
        d["bot"] = Bot.from_dict(d["bot"])
        d["translations"] = Translations.from_dict(d.get("translations") or {})
        d["equity_curve"] = [EquityPoint.from_dict(p) for p in d.get("equity_curve", [])]
        if d.get("today") is not None:
            d["today"] = Day.from_dict(d["today"])
        d["today_trades"] = [Trade.from_dict(t) for t in d.get("today_trades", [])]
        d["recent_days"] = [Day.from_dict(s) for s in d.get("recent_days", [])]
        d["recent_trades"] = [Trade.from_dict(t) for t in d.get("recent_trades", [])]
        d["personas"] = [Persona.from_dict(p) for p in d.get("personas", [])]
        if d.get("cage_match") is not None:
            d["cage_match"] = CageMatch.from_dict(d["cage_match"])
        if d.get("eod_reflection") is not None:
            d["eod_reflection"] = EodReflection.from_dict(d["eod_reflection"])
        return cls(**_strict(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
