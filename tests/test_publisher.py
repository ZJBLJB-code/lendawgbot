"""Real tests for tools/publisher.py.

Each test creates an isolated quant-bot stub under ``tmp_path`` and
exercises the publisher end-to-end. We never read the actual filesystem
under ``/Users/...`` so the tests are deterministic.
"""
from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from unittest import mock

import pytest

# Make the project root importable.
ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import publisher  # noqa: E402
from tools.publisher_schema import Dashboard  # noqa: E402


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _write_verdict(path: Path, *, all_pass: bool = True, instrument: str = "MES",
                   ts: str = "2026-04-27T02:00:00+00:00") -> Path:
    """Write a verdict_*.json file in the canonical quant-bot shape."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "ts": ts,
        "phase": "day0",
        "instrument": instrument,
        "data": {"start": "2021-04-25", "end": "2026-04-25", "rth_bars": 487134},
        "wfo": {
            "n_folds": 12, "n_combos": 3, "winner": "ORF1.5_VWAP1.5",
            "holdout_start": "2025-04-23", "holdout_end": "2026-04-23",
        },
        "gate": {
            "all_pass": all_pass,
            "g1_pass": all_pass, "g2_pass": all_pass, "g3_pass": all_pass,
            "g4_pass": all_pass, "g5_pass": all_pass,
            "in_sample_sharpe": 1.02 if all_pass else 0.4,
            "oos_sharpe": 0.86 if all_pass else 0.3,
            "deflated_sharpe": 0.69 if all_pass else 0.1,
            "pbo": 0.22 if all_pass else 0.5,
            "holdout_sharpe": 1.10 if all_pass else 0.2,
        },
    }
    path.write_text(json.dumps(payload))
    return path


def _make_root(tmp_path: Path) -> Path:
    """Build an empty quant-bot stub under tmp_path/qbot."""
    root = tmp_path / "qbot"
    (root / "journal" / "day0").mkdir(parents=True)
    (root / "journal" / "live").mkdir(parents=True)
    return root


# ---------------------------------------------------------------------------
# DAY0 — canonical picker + freshness
# ---------------------------------------------------------------------------


def test_day0_mode_with_canonical_verdict(tmp_path: Path) -> None:
    """A single canonical verdict file → mode=DAY0, all_pass=True."""
    root = _make_root(tmp_path)
    _write_verdict(root / "journal" / "day0" / "verdict_2026-04-27.json", all_pass=True)
    out = tmp_path / "dashboard.json"

    payload = publisher.publish(root, out, mode="AUTO")

    assert payload["mode"] == "DAY0"
    assert payload["verdict"]["all_pass"] is True
    assert payload["verdict"]["verdict"] == "PASS"
    assert payload["verdict"]["gates_passed"] == 5
    assert payload["schema_version"] == "2.0"
    # File on disk matches.
    on_disk = json.loads(out.read_text())
    assert on_disk == payload


def test_day0_mode_picks_canonical_over_variants(tmp_path: Path) -> None:
    """Canonical verdict_<DATE>.json wins even when variants are newer."""
    root = _make_root(tmp_path)
    day0 = root / "journal" / "day0"

    # Canonical (older mtime).
    canonical = _write_verdict(day0 / "verdict_2026-04-25.json", all_pass=True)
    old_time = canonical.stat().st_mtime - 10_000
    os.utime(canonical, (old_time, old_time))

    # Five suffixed variants (newer mtimes), each FAIL so we can detect a wrong pick.
    for slug in ("trend_only", "MNQ", "russell_reconstitution", "quarter_start_drift", "scalp"):
        v = _write_verdict(day0 / f"verdict_{slug}_2026-04-26.json", all_pass=False)
        assert v.stat().st_mtime > old_time

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="DAY0")
    # If we picked a variant, all_pass would be False — assert canonical won.
    assert payload["verdict"]["all_pass"] is True
    assert payload["verdict"]["gates_passed"] == 5


def test_day0_mode_falls_back_to_freshest_variant_when_no_canonical(tmp_path: Path) -> None:
    """No canonical → pick the freshest variant by date+mtime."""
    root = _make_root(tmp_path)
    day0 = root / "journal" / "day0"

    older = _write_verdict(day0 / "verdict_alpha_2026-04-20.json", all_pass=False)
    newer = _write_verdict(day0 / "verdict_beta_2026-04-26.json", all_pass=True)
    # Make the date order unambiguous via mtime too.
    os.utime(older, (older.stat().st_mtime - 1000, older.stat().st_mtime - 1000))
    os.utime(newer, (newer.stat().st_mtime, newer.stat().st_mtime))

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="DAY0")
    # The 2026-04-26 variant has the newer date; assert we got its all_pass.
    assert payload["verdict"]["all_pass"] is True


# ---------------------------------------------------------------------------
# LIVE — mode trigger
# ---------------------------------------------------------------------------


def test_live_mode_triggers_on_real_fill(tmp_path: Path) -> None:
    """A live JSONL with a bracket_filled event → mode=LIVE."""
    root = _make_root(tmp_path)
    live = root / "journal" / "live" / "2026-04-27.jsonl"
    live.write_text(
        '{"ts":"2026-04-27T15:00:00+00:00","event":"heartbeat"}\n'
        '{"ts":"2026-04-27T15:30:00+00:00","event":"bracket_filled",'
        '"bracket_id":"BR-1","sub_signal":"ORF","qty":1,"entry":5400.0}\n'
    )
    # Touch to keep mtime fresh (< 30 hours).
    _write_verdict(root / "journal" / "day0" / "verdict_2026-04-27.json", all_pass=True)

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="AUTO")

    assert payload["mode"] == "LIVE"
    assert payload["verdict"] is None
    assert payload["cumulative"] is not None


def test_live_mode_reconstructs_trade_from_fill_pairs(tmp_path: Path) -> None:
    """A bracket with entry-leg fill + exit-leg fill + position_closed must
    reconstruct cleanly into a Trade. This is today's actual bot shape —
    real fills land before the parquet build job catches up.
    """
    root = _make_root(tmp_path)
    live = root / "journal" / "live" / "2026-05-15.jsonl"
    live.write_text(
        '{"ts":"2026-05-15T15:50:00+00:00","event":"bracket_submitted",'
        '"bracket_id":"BR-AAA","sub_signal":"ORF","side":"LONG","qty":1,'
        '"entry":7436.5,"stop":7430.0,"target":7447.5,"instrument":"MES"}\n'
        '{"ts":"2026-05-15T15:51:11+00:00","event":"fill","bracket_id":"BR-AAA",'
        '"leg":"entry","action":"BUY","qty":1,"price":7436.5}\n'
        '{"ts":"2026-05-15T16:21:24+00:00","event":"fill","bracket_id":"BR-AAA",'
        '"leg":"target","action":"SELL","qty":1,"price":7447.5}\n'
        '{"ts":"2026-05-15T16:21:24+00:00","event":"position_closed",'
        '"bracket_id":"BR-AAA","exit_leg":"target","exit_price":7447.5,'
        '"net_pnl_usd":55.0,"equity_after_usd":5990.2}\n'
    )

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="AUTO")

    assert payload["mode"] == "LIVE"
    assert payload["awaiting_first_trade"] is False
    today_trades = payload["today_trades"]
    assert len(today_trades) == 1
    t = today_trades[0]
    assert t["trade_id"] == "BR-AAA"
    assert t["direction"] == "LONG"
    assert t["entry_price"] == 7436.5
    assert t["exit_price"] == 7447.5
    assert t["pnl_dollars"] == 55.0
    assert t["exit_reason"] == "TARGET"
    assert t["bars_held"] == 30  # 30 minutes between entry-fill and exit-fill
    assert t["sub_signal"] == "ORF"
    # PII keys never propagate.
    s = json.dumps(payload)
    assert "equity_after_usd" not in s
    assert "net_pnl_usd" not in s


def test_live_mode_awaiting_first_trade_when_no_closes(tmp_path: Path) -> None:
    """A live JSONL with bracket_filled but no closed positions still trips
    LIVE mode, but the dashboard must signal awaiting_first_trade=True so
    the renderer shows the empty-state banner instead of a blank hero.
    """
    root = _make_root(tmp_path)
    live = root / "journal" / "live" / "2026-05-15.jsonl"
    live.write_text(
        '{"ts":"2026-05-15T15:00:00+00:00","event":"heartbeat"}\n'
        # bracket_filled trips LIVE mode but there's no exit fill or close —
        # so reconstruction can't synthesize a closed trade.
        '{"ts":"2026-05-15T15:30:00+00:00","event":"bracket_filled",'
        '"bracket_id":"BR-OPEN","sub_signal":"ORF","qty":1,"entry":5400.0}\n'
    )

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="AUTO")

    assert payload["mode"] == "LIVE"
    assert payload["today_trades"] == []
    assert payload["awaiting_first_trade"] is True


def test_live_mode_ignores_sat_test_brackets(tmp_path: Path) -> None:
    """A live JSONL with only SAT_TEST brackets must NOT promote to LIVE."""
    root = _make_root(tmp_path)
    live = root / "journal" / "live" / "2026-04-27.jsonl"
    live.write_text(
        '{"ts":"2026-04-27T15:00:00+00:00","event":"heartbeat"}\n'
        '{"ts":"2026-04-27T15:30:00+00:00","event":"bracket_submitted",'
        '"bracket_id":"BR-SAT","sub_signal":"SAT_TEST","qty":1,"entry":5400.0}\n'
        '{"ts":"2026-04-27T15:30:01+00:00","event":"fill",'
        '"bracket_id":"BR-SAT","sub_signal":"SAT_TEST","leg":"entry","qty":1,"price":5400.0}\n'
    )
    _write_verdict(root / "journal" / "day0" / "verdict_2026-04-27.json", all_pass=True)

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="AUTO")

    assert payload["mode"] == "DAY0"


# ---------------------------------------------------------------------------
# PII red-team
# ---------------------------------------------------------------------------


def test_pii_never_leaves(tmp_path: Path) -> None:
    """Canonical PII tokens MUST NOT appear in the published JSON.

    Inputs are deliberately laced with real-money identifiers; the output
    must be clean by virtue of the schema allow-list — no separate strip step.
    """
    root = _make_root(tmp_path)
    # Verdict with extra PII-adjacent fields.
    verdict = root / "journal" / "day0" / "verdict_2026-04-27.json"
    verdict_payload = {
        "ts": "2026-04-27T02:00:00+00:00",
        "phase": "day0",
        "instrument": "MES",
        "account_id": "U25438416",          # PII
        "client_id": 1,                     # PII
        "host": "127.0.0.1",                # PII
        "port": 4001,                       # PII
        "snapshot": {"equity_usd": 1100.0}, # PII (nested)
        "data": {"start": "2021-04-25", "end": "2026-04-25", "rth_bars": 487134},
        "wfo": {
            "n_folds": 12, "n_combos": 3, "winner": "ORF1.5_VWAP1.5",
            "holdout_start": "2025-04-23", "holdout_end": "2026-04-23",
        },
        "gate": {
            "all_pass": True,
            "g1_pass": True, "g2_pass": True, "g3_pass": True,
            "g4_pass": True, "g5_pass": True,
            "in_sample_sharpe": 1.02, "oos_sharpe": 0.86,
            "deflated_sharpe": 0.69, "pbo": 0.22, "holdout_sharpe": 1.10,
        },
    }
    verdict.write_text(json.dumps(verdict_payload))

    # Live log laced with PII (won't promote to LIVE — no real trade event).
    live = root / "journal" / "live" / "2026-04-27.jsonl"
    live.write_text(
        '{"ts":"2026-04-27T15:00:00+00:00","event":"broker_connected",'
        '"host":"127.0.0.1","port":4001,"client_id":1,'
        '"account_id":"U25438416","trading_mode":"live"}\n'
        '{"ts":"2026-04-27T15:01:00+00:00","event":"heartbeat",'
        '"equity_usd":1100.0,"drawdown_pct":0.0,"open_position":null}\n'
        '{"ts":"2026-04-27T15:02:00+00:00","event":"orchestrator_starting",'
        '"starting_equity_usd":1100.0,"account_id":"DUP187165"}\n'
    )

    out = tmp_path / "dashboard.json"
    publisher.publish(root, out, mode="AUTO")

    rendered = out.read_text()
    forbidden = [
        "U25438416",
        "DUP187165",
        "equity_usd",
        "starting_equity_usd",
        "drawdown_pct",
        "account_id",
        "client_id",
        "broker_connected",
        "127.0.0.1",
    ]
    for token in forbidden:
        assert token not in rendered, f"PII leaked: {token!r} in published JSON"


# ---------------------------------------------------------------------------
# Schema round-trip
# ---------------------------------------------------------------------------


def test_schema_validation_round_trip(tmp_path: Path) -> None:
    """to_dict → from_dict → to_dict must be idempotent."""
    root = _make_root(tmp_path)
    _write_verdict(root / "journal" / "day0" / "verdict_2026-04-27.json", all_pass=True)
    out = tmp_path / "dashboard.json"

    publisher.publish(root, out, mode="DAY0")

    loaded = json.loads(out.read_text())
    rehydrated = Dashboard.from_dict(loaded)
    redumped = rehydrated.to_dict()
    assert redumped == loaded


# ---------------------------------------------------------------------------
# Hardening: corrupt / mid-write verdict files
# ---------------------------------------------------------------------------


def test_corrupt_canonical_falls_through_to_valid_variant(tmp_path: Path) -> None:
    """A truncated canonical verdict must be skipped in favour of a parseable variant.

    Regression for red-team P1: previously ``_read_json`` returned None on a
    truncated file, and ``_build_day0`` silently emitted a zero-gates Lab
    verdict. Now the picker drops corrupt files before they reach the builder.
    """
    root = _make_root(tmp_path)
    day0 = root / "journal" / "day0"
    # Canonical: truncated mid-write.
    (day0 / "verdict_2026-04-27.json").write_text('{"ts":"2026-04-27","gate":')
    # Variant: valid, all_pass=False so we can prove which one was chosen.
    _write_verdict(day0 / "verdict_trend_only_2026-04-27.json", all_pass=False)

    out = tmp_path / "dashboard.json"
    payload = publisher.publish(root, out, mode="DAY0")
    # If the corrupt canonical had been chosen, gates_passed would be 0.
    # The valid variant has all_pass=False but its gates dict is shaped right
    # so gates_passed should be 0 too — the discriminator is provenance.
    prov = payload.get("_provenance") or {}
    assert prov["source_detail"] == "verdict_trend_only_2026-04-27.json"


def test_all_corrupt_verdicts_raises_publisher_error(tmp_path: Path) -> None:
    """If every verdict file is corrupt, the publisher must fail loudly."""
    root = _make_root(tmp_path)
    day0 = root / "journal" / "day0"
    (day0 / "verdict_2026-04-27.json").write_text('{"truncated":')
    (day0 / "verdict_alpha_2026-04-27.json").write_text("garbage")

    out = tmp_path / "dashboard.json"
    with pytest.raises(publisher.PublisherError):
        publisher.publish(root, out, mode="DAY0")


# ---------------------------------------------------------------------------
# Atomic write
# ---------------------------------------------------------------------------


def test_atomic_write_no_partial_file(tmp_path: Path) -> None:
    """A failure mid-write must NOT leave a partial file at the destination.

    Strategy: pre-seed the destination with a known-good payload, then
    monkeypatch ``os.replace`` to raise. The publisher should clean up the
    temp file and the destination must still hold the original content.
    """
    root = _make_root(tmp_path)
    _write_verdict(root / "journal" / "day0" / "verdict_2026-04-27.json", all_pass=True)
    out = tmp_path / "dashboard.json"

    # Previous-good payload on disk.
    out.write_text(json.dumps({"sentinel": "previous-good"}))
    original = out.read_text()

    with mock.patch("tools.publisher.os.replace", side_effect=OSError("simulated crash")):
        with pytest.raises(OSError):
            publisher.publish(root, out, mode="DAY0")

    # Destination is untouched.
    assert out.read_text() == original
    # No leftover temp files.
    leftover = list(tmp_path.glob(".dashboard.json.*"))
    assert not leftover, f"temp files leaked: {leftover}"
