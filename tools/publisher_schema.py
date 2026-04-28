"""Canonical Dashboard schema (publisher v2.0).

This is the single source of truth for what the Len Dawg Bot dashboard renders.
The publisher emits exactly this shape; the renderer reads exactly this shape.
Anything not declared here MUST NOT appear in published JSON.

Design choice: stdlib dataclasses (Python 3.9-compatible). Pydantic v2 would
be cleaner but it's not in the runtime environment. The dataclass pattern
mirrors ``schema.py`` so existing readers keep working.

The PII story: this module is the *allow-list*. Field names below are the
contract. Constructing a Dashboard via the publisher cannot leak PII because
unknown keys are silently dropped by ``_strict()`` in every ``from_dict``.
That's the architectural inversion vs ``scripts/sanitize_jsonl.py`` (a
blocklist on raw events).
"""
from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, fields, is_dataclass
from typing import Any, ClassVar

SCHEMA_VERSION = "2.0"
PUBLISHER_VERSION = "1.0.0"


def _strict(cls: Any, d: dict[str, Any]) -> dict[str, Any]:
    """Drop unknown keys — the allow-list enforcement point."""
    names = {f.name for f in fields(cls)}
    return {k: v for k, v in d.items() if k in names}


# --- Atomic types -----------------------------------------------------------


@dataclass(frozen=True)
class Bot:
    name: str
    instrument: str
    phase_short: str
    instrument_friendly: str | None = None
    phase: str | None = None
    days_live: int | None = None
    starting_equity: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Bot":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class PersonaVote:
    persona: str
    verdict: str  # TAKE | SKIP | REDUCE_SIZE
    confidence: float
    reasoning: str
    size_multiplier: float = 1.0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PersonaVote":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Trade:
    trade_id: str
    date: str
    entry_time: str
    exit_time: str
    direction: str
    sub_signal: str
    regime: str
    qty: int
    entry_price: float
    exit_price: float
    pnl_dollars: float
    pnl_points: float
    bars_held: int
    exit_reason: str
    persona_votes: list[PersonaVote] = field(default_factory=list)
    stop_price: float | None = None
    target_price: float | None = None
    thesis_confirmed: bool | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Trade":
        d = _strict(cls, dict(d))
        d["persona_votes"] = [PersonaVote.from_dict(v) for v in d.get("persona_votes", [])]
        return cls(**d)


@dataclass(frozen=True)
class Day:
    date: str
    regime: str
    trades_count: int
    wins: int
    losses: int
    pnl_dollars: float
    cumulative_pnl: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Day":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class EquityPoint:
    date: str
    equity: float
    pnl: float

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "EquityPoint":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class CageMatch:
    trade_id: str
    outcome: str
    pnl_dollars: float
    direction: str
    sub_signal: str
    regime: str
    votes: list[PersonaVote] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "CageMatch":
        d = _strict(cls, dict(d))
        d["votes"] = [PersonaVote.from_dict(v) for v in d.get("votes", [])]
        return cls(**d)


@dataclass(frozen=True)
class Persona:
    code: str
    name: str
    nickname: str
    wins: int = 0
    losses: int = 0
    vetoed: int = 0

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Persona":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Translations:
    regime: dict[str, str] = field(default_factory=dict)
    sub_signal: dict[str, str] = field(default_factory=dict)
    exit_reason: dict[str, str] = field(default_factory=dict)
    persona_nicknames: dict[str, str] = field(default_factory=dict)
    persona_names: dict[str, str] = field(default_factory=dict)
    instrument_friendly: dict[str, str] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Translations":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Cumulative:
    total_pnl: float
    total_trades: int
    win_rate: float
    current_equity: float
    streak: int = 0
    streak_type: str = "none"

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Cumulative":
        return cls(**_strict(cls, d))


@dataclass(frozen=True)
class Gate:
    id: str
    name: str
    threshold: float
    pass_: bool = field(metadata={"json_key": "pass"})
    value: float | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Gate":
        d = dict(d)
        # Accept both "pass" (json) and "pass_" (python) for the boolean.
        if "pass" in d and "pass_" not in d:
            d["pass_"] = d.pop("pass")
        return cls(**_strict(cls, d))

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "threshold": self.threshold,
            "pass": self.pass_,
            "value": self.value,
        }


@dataclass(frozen=True)
class Verdict:
    verdict: str  # PASS | FAIL | PENDING
    headline: str
    gates_passed: int
    gates: list[Gate] = field(default_factory=list)
    all_pass: bool = False
    wfo_winner: str | None = None
    holdout_start: str | None = None
    holdout_end: str | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Verdict":
        d = _strict(cls, dict(d))
        d["gates"] = [Gate.from_dict(g) for g in d.get("gates", [])]
        return cls(**d)

    def to_dict(self) -> dict[str, Any]:
        return {
            "verdict": self.verdict,
            "headline": self.headline,
            "gates_passed": self.gates_passed,
            "all_pass": self.all_pass,
            "wfo_winner": self.wfo_winner,
            "holdout_start": self.holdout_start,
            "holdout_end": self.holdout_end,
            "gates": [g.to_dict() for g in self.gates],
        }


@dataclass(frozen=True)
class Provenance:
    source: str
    source_detail: str
    data_as_of: str | None = None
    publisher_run_id: str = ""

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Provenance":
        return cls(**_strict(cls, d))


# --- Top-level envelope -----------------------------------------------------


@dataclass(frozen=True)
class Dashboard:
    """The complete published payload. v2.0 of the schema.

    Only fields below are emitted. Source data is allow-listed at construction
    time — anything PII-adjacent (account_id, equity_usd, host, port, etc.)
    cannot survive the trip into this object because no field in this schema
    accepts it.
    """
    SCHEMA_VERSION: ClassVar[str] = SCHEMA_VERSION

    schema_version: str
    publisher_version: str
    generated_at: str
    mode: str
    bot: Bot
    translations: Translations
    personas: dict[str, Persona] = field(default_factory=dict)
    equity_curve: list[EquityPoint] = field(default_factory=list)
    today_trades: list[Trade] = field(default_factory=list)
    today: Day | None = None
    cumulative: Cumulative | None = None
    cage_match: CageMatch | None = None
    verdict: Verdict | None = None
    as_of_date: str | None = None
    ai_learnings: list[str] = field(default_factory=list)
    tomorrow_watch: list[str] = field(default_factory=list)
    _provenance: Provenance | None = None

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Dashboard":
        d = dict(d)
        d.setdefault("schema_version", cls.SCHEMA_VERSION)
        d.setdefault("publisher_version", PUBLISHER_VERSION)
        d["bot"] = Bot.from_dict(d["bot"])
        d["translations"] = Translations.from_dict(d.get("translations") or {})
        d["personas"] = {
            k: Persona.from_dict(v) for k, v in (d.get("personas") or {}).items()
        }
        d["equity_curve"] = [EquityPoint.from_dict(p) for p in d.get("equity_curve") or []]
        d["today_trades"] = [Trade.from_dict(t) for t in d.get("today_trades") or []]
        if d.get("today") is not None:
            d["today"] = Day.from_dict(d["today"])
        if d.get("cumulative") is not None:
            d["cumulative"] = Cumulative.from_dict(d["cumulative"])
        if d.get("cage_match") is not None:
            d["cage_match"] = CageMatch.from_dict(d["cage_match"])
        if d.get("verdict") is not None:
            d["verdict"] = Verdict.from_dict(d["verdict"])
        if d.get("_provenance") is not None:
            d["_provenance"] = Provenance.from_dict(d["_provenance"])
        return cls(**_strict(cls, d))

    def to_dict(self) -> dict[str, Any]:
        """Serialise to a plain JSON-safe dict.

        We can't use ``dataclasses.asdict`` blindly because Gate has the
        ``pass_`` -> ``pass`` rename, and Verdict nests Gates.
        """
        d = asdict(self)
        if self.verdict is not None:
            d["verdict"] = self.verdict.to_dict()
        if self.cage_match is not None:
            d["cage_match"] = asdict(self.cage_match)
        return d


# --- JSON Schema export -----------------------------------------------------


def _py_to_json_type(annotation: str) -> dict[str, Any]:
    """Map a stringified type annotation to a JSON Schema fragment.

    Best-effort. Used only for the static JSON Schema export — runtime
    validation goes through the dataclass round-trip in ``publisher.py``.
    """
    a = annotation.replace(" ", "")
    nullable = False
    if a.endswith("|None"):
        a = a[: -len("|None")]
        nullable = True
    base: dict[str, Any]
    if a == "str":
        base = {"type": "string"}
    elif a == "int":
        base = {"type": "integer"}
    elif a == "float":
        base = {"type": "number"}
    elif a == "bool":
        base = {"type": "boolean"}
    elif a.startswith("list["):
        inner = a[5:-1]
        base = {"type": "array", "items": _py_to_json_type(inner)}
    elif a.startswith("dict["):
        # dict[str,X]
        inner = a[5:-1].split(",", 1)[1] if "," in a[5:-1] else "Any"
        base = {"type": "object", "additionalProperties": _py_to_json_type(inner)}
    else:
        # Treat any other reference (Bot, Persona, Trade, …) as an object ref.
        base = {"$ref": f"#/$defs/{a}"}
    if nullable:
        return {"anyOf": [base, {"type": "null"}]}
    return base


def _dataclass_to_schema(cls: Any) -> dict[str, Any]:
    """Render a dataclass as a JSON Schema object."""
    props: dict[str, Any] = {}
    required: list[str] = []
    for f in fields(cls):
        name = f.name
        # Honour the Gate.pass_ -> pass rename in the public schema.
        json_name = name
        if cls.__name__ == "Gate" and name == "pass_":
            json_name = "pass"
        ann = str(f.type)
        props[json_name] = _py_to_json_type(ann)
        if f.default is f.default_factory is None:  # type: ignore[comparison-overlap]
            required.append(json_name)
    return {
        "type": "object",
        "title": cls.__name__,
        "properties": props,
        "additionalProperties": False,
        **({"required": required} if required else {}),
    }


def _all_dataclasses() -> dict[str, Any]:
    return {
        c.__name__: _dataclass_to_schema(c)
        for c in (
            Bot, PersonaVote, Trade, Day, EquityPoint, CageMatch,
            Persona, Translations, Cumulative, Gate, Verdict, Provenance,
        )
        if is_dataclass(c)
    }


def dump_json_schema() -> str:
    """Return the JSON Schema (draft 2020-12) for the Dashboard envelope."""
    schema = {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://lendawgbot.io/schemas/dashboard-2.0.json",
        "title": "Dashboard",
        "description": (
            "Canonical Len Dawg Bot dashboard payload, produced by "
            "tools/publisher.py. Schema version 2.0."
        ),
        "$defs": _all_dataclasses(),
        **_dataclass_to_schema(Dashboard),
    }
    return json.dumps(schema, indent=2)


if __name__ == "__main__":
    print(dump_json_schema())
