from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_pure_momentum_paper import (
    PaperOrderPlan,
    anomaly_guard_report,
    cap_buy_to_current_buying_power,
    execution_drift_report,
    forward_lock_report,
    regime_defense_report,
)


def test_forward_lock_passes_matching_profile() -> None:
    strategy = {
        "name": "pure_momentum_cap_phase_blend_gross12_mid13",
        "lookback_days": 63,
        "top_n": 7,
        "rebalance_days": 7,
        "target_gross_leverage": 1.2,
        "max_gross_leverage": 1.3,
        "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
        "phase_sleeves": [
            {"name": "phase0_core", "allocation": 0.85},
            {"name": "phase1_diversifier", "allocation": 0.15},
        ],
    }
    evidence = {
        "forward_lock": {
            "enabled": True,
            "strategy_name": "pure_momentum_cap_phase_blend_gross12_mid13",
            "lookback_days": 63,
            "top_n": 7,
            "rebalance_days": 7,
            "target_gross_leverage": 1.2,
            "max_gross_leverage": 1.3,
            "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
            "phase_sleeves": {"phase0_core": 0.85, "phase1_diversifier": 0.15},
        }
    }

    report = forward_lock_report(strategy, evidence)

    assert report["passed"] is True
    assert report["mismatches"] == []


def test_forward_lock_blocks_changed_allocation() -> None:
    strategy = {
        "name": "pure_momentum_cap_phase_blend_gross12_mid13",
        "lookback_days": 63,
        "top_n": 7,
        "rebalance_days": 7,
        "target_gross_leverage": 1.2,
        "max_gross_leverage": 1.3,
        "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
        "phase_sleeves": [
            {"name": "phase0_core", "allocation": 0.70},
            {"name": "phase1_diversifier", "allocation": 0.30},
        ],
    }
    evidence = {
        "forward_lock": {
            "enabled": True,
            "strategy_name": "pure_momentum_cap_phase_blend_gross12_mid13",
            "lookback_days": 63,
            "top_n": 7,
            "rebalance_days": 7,
            "target_gross_leverage": 1.2,
            "max_gross_leverage": 1.3,
            "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
            "phase_sleeves": {"phase0_core": 0.85, "phase1_diversifier": 0.15},
        }
    }

    report = forward_lock_report(strategy, evidence)

    assert report["passed"] is False
    assert report["mismatches"][0]["field"] == "phase_sleeves"


def test_forward_lock_checks_quantum_sleeve() -> None:
    strategy = {
        "name": "pure_momentum_cap_phase_blend_gross12_mid12_quantum5",
        "lookback_days": 63,
        "top_n": 7,
        "rebalance_days": 7,
        "target_gross_leverage": 1.2,
        "max_gross_leverage": 1.2,
        "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
        "phase_sleeves": [
            {"name": "phase0_core", "allocation": 0.8075},
            {"name": "phase1_diversifier", "allocation": 0.1425},
        ],
        "quantum_sleeve": {
            "enabled": True,
            "allocation": 0.05,
            "symbols": ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ"],
        },
    }
    evidence = {
        "forward_lock": {
            "enabled": True,
            "strategy_name": "pure_momentum_cap_phase_blend_gross12_mid12_quantum5",
            "lookback_days": 63,
            "top_n": 7,
            "rebalance_days": 7,
            "target_gross_leverage": 1.2,
            "max_gross_leverage": 1.2,
            "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
            "phase_sleeves": {"phase0_core": 0.8075, "phase1_diversifier": 0.1425},
            "quantum_sleeve": {
                "enabled": True,
                "allocation": 0.05,
                "symbols": ["IONQ", "RGTI", "QBTS", "QUBT", "ARQQ"],
            },
        }
    }

    report = forward_lock_report(strategy, evidence)

    assert report["passed"] is True
    strategy["quantum_sleeve"]["symbols"] = ["IONQ"]
    report = forward_lock_report(strategy, evidence)
    assert report["passed"] is False
    assert report["mismatches"][0]["field"] == "quantum_sleeve"


def test_regime_defense_activates_only_hard_bear_regime() -> None:
    dates = pd.bdate_range("2025-01-01", periods=220)
    close = pd.DataFrame(
        {
            "SPY": [500.0] * 94 + [440.0] * 126,
            "QQQ": [400.0] * 94 + [340.0] * 126,
            "GLD": [200.0] * 220,
        },
        index=dates,
    )
    strategy = {
        "regime_defense": {
            "enabled": True,
            "defensive_symbol": "GLD",
            "benchmark_symbol": "SPY",
            "confirmation_symbol": "QQQ",
            "ma_days": 200,
            "momentum_days": 126,
            "momentum_threshold": -0.10,
            "target_gross": 1.0,
        }
    }

    report = regime_defense_report(close, strategy)

    assert report["active"] is True
    assert report["defensive_symbol"] == "GLD"
    assert report["target_gross"] == 1.0


def test_anomaly_guard_blocks_absurd_latest_return() -> None:
    close = pd.DataFrame(
        {
            "AAA": [100.0, 250.0],
            "SPY": [100.0, 101.0],
        },
        index=pd.to_datetime(["2026-05-26", pd.Timestamp.now(tz="America/New_York").date()]),
    )
    evidence = {
        "anomaly_guard": {
            "enabled": True,
            "max_completed_data_stale_days": 5,
            "max_abs_daily_return": 0.85,
            "min_latest_universe_coverage": 0.90,
        }
    }

    report = anomaly_guard_report(close, ["AAA", "SPY"], evidence)

    assert report["passed"] is False
    assert any(issue["type"] == "absurd_latest_daily_return" for issue in report["issues"])


def test_execution_drift_passes_filled_targets() -> None:
    orders = [
        {
            "symbol": "AAA",
            "side": "buy",
            "state": "filled",
            "notional_estimate": 100.0,
            "filled_qty": "10",
            "filled_avg_price": "10",
        }
    ]
    evidence = {"execution_drift": {"enabled": True, "max_symbol_qty_drift": 1, "max_open_orders_after_run": 0}}

    report = execution_drift_report(orders, {"AAA": 10}, {"AAA": 10}, 0, evidence)

    assert report["passed"] is True
    assert report["buy_fill_ratio"] == 1.0


def test_buying_power_cap_uses_buying_power_when_daytrading_power_zero() -> None:
    class FakeClient:
        def get_account(self) -> dict[str, str]:
            return {"buying_power": "200000", "daytrading_buying_power": "0"}

    plan = PaperOrderPlan(symbol="AMD", side="buy", qty=61, price=500.0, target_qty=61)

    capped, info = cap_buy_to_current_buying_power(FakeClient(), plan)

    assert capped is not None
    assert capped.qty == 61
    assert info["effective_buying_power"] == 200000.0
    assert info["applied"] is False
