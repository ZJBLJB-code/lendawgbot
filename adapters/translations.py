"""Static presentation-layer dicts for the TomCash dashboard.

These are owned by TomCash, not quant-bot. quant-bot emits machine codes
(e.g. ``MEAN_REV_FRIENDLY``); the dashboard layer translates those into
plain-English copy for older-adult readers per the v3 IA spec.

Strings here must match `data/sample.json` exactly so sample renders and
adapter renders look identical.
"""
from __future__ import annotations

from typing import Final

REGIME_TRANSLATIONS: Final[dict[str, str]] = {
    "MEAN_REV_FRIENDLY": "Calm, choppy market",
    "TREND_REGIME": "Strong trend day",
    "VOLATILE": "Choppy, wild market",
    "UNCLASSIFIED": "Unclear regime",
}

SUB_SIGNAL_TRANSLATIONS: Final[dict[str, str]] = {
    "ORF": "Opening fade",
    "VWAP_REV": "Pullback",
    "GAP_FILL": "Gap fill",
}

EXIT_REASON_TRANSLATIONS: Final[dict[str, str]] = {
    "TARGET": "hit goal",
    "STOP": "stopped out",
    "TIME": "ran out of time",
    "TRAILING": "trailed out",
}

PERSONA_NICKNAMES: Final[dict[str, str]] = {
    "TT": "The Chartist",
    "ME": "The Macro Guy",
    "OF": "The Flow Reader",
    "DA": "The Skeptic",
}

PERSONA_NAMES: Final[dict[str, str]] = {
    "TT": "Technical Trader",
    "ME": "Macro Economist",
    "OF": "Options Analyst",
    "DA": "Devil's Advocate",
}

INSTRUMENT_FRIENDLY: Final[dict[str, str]] = {
    "/MES": "S&P 500 mini",
    "/ES": "S&P 500 futures",
    "/MNQ": "Nasdaq 100 mini",
    "/NQ": "Nasdaq 100 futures",
    "/MYM": "Dow mini",
    "/YM": "Dow futures",
    "/M2K": "Russell 2000 mini",
    "/RTY": "Russell 2000 futures",
}
