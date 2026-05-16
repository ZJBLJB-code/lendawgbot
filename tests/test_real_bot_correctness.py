"""Run the publisher against pinned real-journal fixtures, assert exact numbers.

Fixtures live under tests/fixtures/real_journals/journal/live/*.jsonl and are
PII-stripped real bot output. The expected numbers below were computed by
hand from those fixtures on 2026-05-15.

When intentionally re-pinning the fixtures (e.g. adding a new month), update
EXPECTED below in the same commit so the diff is auditable.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from tools import publisher  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "real_journals"

# Hand-verified ground truth from the fixture journals on 2026-05-15.
# To re-pin: run `python3 tools/publisher.py tests/fixtures/real_journals`,
# verify the printed summary by eye against the journals, copy here.
EXPECTED = {
    "starting_equity": 5992.21,
    "current_equity":  5960.20,
    "account_change_usd":     -32.01,
    "realized_trade_pnl_usd":  80.00,
    "cost_drift_usd":         112.01,
    "total_trades": 10,
    "wins":          4,
    "losses":        6,
}


@pytest.fixture(scope="module")
def dashboard():
    return publisher.build_dashboard(str(FIXTURE))


def test_starting_equity(dashboard):
    assert dashboard["bot"]["starting_equity"] == pytest.approx(EXPECTED["starting_equity"], abs=0.01)


def test_current_equity(dashboard):
    assert dashboard["bot"]["current_equity"] == pytest.approx(EXPECTED["current_equity"], abs=0.01)


def test_account_change(dashboard):
    assert dashboard["headline"]["account_change_usd"] == pytest.approx(EXPECTED["account_change_usd"], abs=0.01)


def test_realized_pnl_sum(dashboard):
    assert dashboard["headline"]["realized_trade_pnl_usd"] == pytest.approx(EXPECTED["realized_trade_pnl_usd"], abs=0.01)


def test_cost_drift(dashboard):
    assert dashboard["headline"]["cost_drift_usd"] == pytest.approx(EXPECTED["cost_drift_usd"], abs=0.01)


def test_trade_counts(dashboard):
    assert dashboard["headline"]["total_trades"] == EXPECTED["total_trades"]
    assert dashboard["headline"]["wins"] == EXPECTED["wins"]
    assert dashboard["headline"]["losses"] == EXPECTED["losses"]
    assert len(dashboard["trades"]) == EXPECTED["total_trades"]


def test_data_quality_gap_surfaced(dashboard):
    # The fixture has a known $112.01 commission drift — the dashboard MUST
    # surface it as a warning so Tom never sees a hidden lie.
    warnings = dashboard.get("data_quality", {}).get("warnings", [])
    assert any("fees/slippage" in w for w in warnings), \
        f"expected commission-drift warning, got: {warnings}"


def test_invariants_hold(dashboard):
    # Re-run invariants explicitly (publisher already does this internally
    # before writing, but assert here to make any future regression loud).
    publisher._check_invariants(dashboard)


def test_no_test_pollution_leaks_into_trades(dashboard):
    for t in dashboard["trades"]:
        assert not t["bracket_id"].startswith("BR-test"), \
            f"test-pollution bracket leaked into trades: {t['bracket_id']}"
