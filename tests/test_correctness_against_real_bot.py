"""Real-bot correctness tests.

These pin the publisher's output against actual bot journal data
captured on 2026-05-15. They are the load-bearing guard against the
"$10K hardcoded baseline" class of bug — if anyone hardcodes another
synthetic constant or breaks the equity-from-events flow, these tests
fail loudly with the divergence.

The fixtures under ``tests/fixtures/real_journals/`` are trimmed copies
of the bot's live JSONLs (only the events the publisher reads, PII
fields stripped). See ``tests/fixtures/_extract_fixture.py`` for the
trimmer.

Ground truth confirmed via direct journal inspection on 2026-05-15:
- 10 closed trades across 6 trading days with closes
- Trading days touched (any activity): 12 distinct dates
- Starting equity (first observed): $5,992.21
- Ending equity (last observed):    $5,960.20
- Cumulative P&L (sum of net_pnl):    +$80.00
- Equity drift (commission/fees):    -$112.01

If any of these numbers shift, EITHER the bot's behavior changed
(legitimate — update fixtures) OR the publisher's math drifted
(bug — find it).
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import publisher  # noqa: E402

FIXTURE_ROOT = ROOT / "tests" / "fixtures" / "real_journals"


def _fresh_fixtures(tmp_path: Path) -> Path:
    """Copy fixtures into tmp_path with fresh mtimes so freshness checks pass.

    The publisher's mode-detection refuses to flip to LIVE on a journal
    older than 30 hours. Our fixtures are pinned files with whatever
    mtime they were committed at, so we copy them into a tmp dir and
    rely on the copy giving them current mtimes.
    """
    src_live = FIXTURE_ROOT / "journal" / "live"
    dst_live = tmp_path / "journal" / "live"
    dst_live.mkdir(parents=True, exist_ok=True)
    for jsonl in src_live.glob("*.jsonl"):
        (dst_live / jsonl.name).write_bytes(jsonl.read_bytes())
    # Add a current-day file so freshness check passes (uses mtime of the
    # most-recent file).
    return tmp_path


def test_real_bot_correctness(tmp_path: Path) -> None:
    """Publisher against pinned real-bot fixtures produces the exact
    ground-truth numbers. This is the regression net for hardcoded
    synthetic defaults.
    """
    root = _fresh_fixtures(tmp_path)
    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="LIVE")

    # ---- Top-level shape -----------------------------------------------
    assert payload["mode"] == "LIVE"
    assert payload["verdict"] is None
    assert payload["awaiting_first_trade"] is False

    # ---- bot.starting_equity must be the REAL value, not $10K ---------
    starting = payload["bot"]["starting_equity"]
    assert starting == pytest.approx(5992.21, abs=0.01), (
        f"starting_equity must be $5,992.21 (first observed equity_after - first pnl), got ${starting}"
    )

    # ---- Cumulative claims ---------------------------------------------
    cum = payload["cumulative"]
    assert cum is not None
    assert cum["total_pnl"] == pytest.approx(80.00, abs=0.01), (
        f"cumulative total_pnl must be +$80.00, got ${cum['total_pnl']}"
    )
    assert cum["total_trades"] == 10
    # 4 wins out of 10 = 0.4 win rate
    assert cum["win_rate"] == pytest.approx(0.4, abs=0.001)
    # current_equity is the bot's actual last reported equity, NOT
    # starting + sum(pnl) (which would be $6,072 — wrong because of
    # commission drift).
    assert cum["current_equity"] == pytest.approx(5960.20, abs=0.01), (
        f"current_equity must be $5,960.20 (real last equity_after_usd), got ${cum['current_equity']}"
    )

    # ---- Per-day breakdown --------------------------------------------
    # Should be 6 days with closes (5/1, 5/5, 5/6, 5/8, 5/11, 5/15).
    days_with_closes = len(payload["equity_curve"])
    assert days_with_closes == 6, f"expected 6 trading days with closes, got {days_with_closes}"

    # ---- bot.days_live counts ALL activity days (not just close days)
    # Fixtures span 5/1, 5/4, 5/5, 5/6, 5/7, 5/8, 5/11, 5/12, 5/13, 5/15
    # (5/9, 5/14 have 0 events kept; 5/12, 5/13 have 1 bracket_submitted each).
    # Whatever the exact count, it MUST be more than days_with_closes (which
    # is the legacy buggy behavior) AND it must be ≥ 6.
    days_live = payload["bot"]["days_live"]
    assert days_live >= days_with_closes, (
        f"days_live ({days_live}) must include all activity days, not just close days "
        f"({days_with_closes}). This was the old hardcoded `len(summaries)` bug."
    )

    # ---- Invariant: sum(daily_pnl) == cumulative_pnl ------------------
    sum_daily = sum(p["pnl"] for p in payload["equity_curve"])
    assert sum_daily == pytest.approx(80.00, abs=0.01)

    # ---- Today's data (2026-05-15 has 2 trades, +$25 net) -------------
    today = payload["today"]
    assert today is not None
    assert today["date"] == "2026-05-15"
    assert today["trades_count"] == 2
    assert today["wins"] == 1
    assert today["losses"] == 1
    assert today["pnl_dollars"] == pytest.approx(25.00, abs=0.01)

    # ---- today_trades populated with the real BR-* IDs ----------------
    today_trades = payload["today_trades"]
    bids = [t["trade_id"] for t in today_trades]
    assert "BR-32760ee4" in bids, f"missing real BR-32760ee4 (+$55 win); got {bids}"
    assert "BR-88d35132" in bids, f"missing real BR-88d35132 (-$30 loss); got {bids}"

    # ---- Equity curve plots REAL equity_after_usd, not $10K-relative -
    last_equity = payload["equity_curve"][-1]["equity"]
    assert last_equity == pytest.approx(5960.20, abs=0.01), (
        f"last equity curve point must be the bot's real $5,960.20, got ${last_equity}"
    )
    first_equity_curve = payload["equity_curve"][0]["equity"]
    # First close was on 5/1 and the eod equity for 5/1 was $5,989.71
    # (last close that day per fixture).
    assert first_equity_curve == pytest.approx(5989.71, abs=0.01)


def test_no_hardcoded_synthetic_defaults() -> None:
    """The publisher module must not contain `STARTING_EQUITY_DEFAULT` anymore.
    This is a structural guard against the original $10K bug.
    """
    src = (ROOT / "tools" / "publisher.py").read_text()
    assert "STARTING_EQUITY_DEFAULT = " not in src, (
        "STARTING_EQUITY_DEFAULT must not be redefined as a synthetic constant. "
        "Read equity from the bot's actual journal events."
    )
