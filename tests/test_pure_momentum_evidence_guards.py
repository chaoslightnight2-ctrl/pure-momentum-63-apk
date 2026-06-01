from __future__ import annotations

import copy
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
    apply_partial_defense_overlay,
    apply_quantum_sleeve_defense,
    partial_defense_report,
    regime_defense_report,
    regime_defense_symbols,
    stateful_regime_signal,
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
            "defense_guard": {
                "enabled": True,
                "name": "quantum_to_bil_on_breadth33_qdd8",
                "defense_symbol": "BIL",
                "trigger": "partial_defense_active",
            },
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
                "defense_guard": {
                    "enabled": True,
                    "name": "quantum_to_bil_on_breadth33_qdd8",
                    "defense_symbol": "BIL",
                    "trigger": "partial_defense_active",
                },
            },
        }
    }

    report = forward_lock_report(strategy, evidence)

    assert report["passed"] is True
    strategy["quantum_sleeve"]["symbols"] = ["IONQ"]
    report = forward_lock_report(strategy, evidence)
    assert report["passed"] is False
    assert report["mismatches"][0]["field"] == "quantum_sleeve"


def test_forward_lock_checks_regime_defense_guard() -> None:
    strategy = {
        "name": "pure_momentum_cap_phase_blend_gross12_mid12_quantum5_pfix_stateful_guard",
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
        "regime_defense_guard": {
            "enabled": True,
            "name": "stateful_2022_pfix_guard",
            "defense_symbol": "PFIX",
            "defense_exposure": 1.0,
            "entry_signal": "early_score2_macro",
            "exit_signal": "bear_score_low",
            "min_hold_days": 10,
            "max_hold_days": 90,
            "cooldown_days": 10,
        },
    }
    evidence = {
        "forward_lock": {
            "enabled": True,
            "strategy_name": "pure_momentum_cap_phase_blend_gross12_mid12_quantum5_pfix_stateful_guard",
            "lookback_days": 63,
            "top_n": 7,
            "rebalance_days": 7,
            "target_gross_leverage": 1.2,
            "max_gross_leverage": 1.2,
            "symbol_weight_caps": {"SOXL": 0.08, "TECL": 0.12},
            "phase_sleeves": {"phase0_core": 0.8075, "phase1_diversifier": 0.1425},
            "regime_defense_guard": copy.deepcopy(strategy["regime_defense_guard"]),
        }
    }

    report = forward_lock_report(strategy, evidence)

    assert report["passed"] is True
    strategy["regime_defense_guard"]["defense_exposure"] = 0.75
    report = forward_lock_report(strategy, evidence)
    assert report["passed"] is False
    assert report["mismatches"][0]["field"] == "regime_defense_guard"


def test_forward_lock_checks_partial_defense_guard() -> None:
    strategy = {
        "name": "pure_momentum_partial_bil50_guard",
        "lookback_days": 63,
        "top_n": 5,
        "rebalance_days": 7,
        "target_gross_leverage": 1.2,
        "max_gross_leverage": 1.2,
        "partial_defense_guard": {
            "enabled": True,
            "name": "breadth33_qdd8_bil50",
            "defense_symbol": "BIL",
            "defense_weight": 0.5,
            "signal": "breadth33_qdd8",
            "hold_days": 3,
            "cooldown_days": 5,
        },
    }
    evidence = {
        "forward_lock": {
            "enabled": True,
            "strategy_name": "pure_momentum_partial_bil50_guard",
            "lookback_days": 63,
            "top_n": 5,
            "rebalance_days": 7,
            "target_gross_leverage": 1.2,
            "max_gross_leverage": 1.2,
            "partial_defense_guard": copy.deepcopy(strategy["partial_defense_guard"]),
        }
    }

    assert forward_lock_report(strategy, evidence)["passed"] is True
    strategy["partial_defense_guard"]["defense_weight"] = 0.25
    report = forward_lock_report(strategy, evidence)
    assert report["passed"] is False
    assert report["mismatches"][0]["field"] == "partial_defense_guard"


def test_regime_defense_symbols_include_market_data_and_defense_symbol() -> None:
    symbols = regime_defense_symbols(
        {
            "regime_defense_guard": {
                "enabled": True,
                "defense_symbol": "PFIX",
                "market_symbols": ["SPY", "QQQ", "TLT", "UUP", "^VIX"],
            }
        }
    )

    assert "PFIX" in symbols
    assert "QQQ" in symbols
    assert "^VIX" in symbols


def test_regime_defense_symbols_include_partial_defense_symbol() -> None:
    symbols = regime_defense_symbols(
        {
            "partial_defense_guard": {
                "enabled": True,
                "defense_symbol": "BIL",
                "market_symbols": ["QQQ"],
            }
        }
    )

    assert "BIL" in symbols
    assert "QQQ" in symbols


def test_stateful_regime_signal_respects_hold_exit_and_cooldown() -> None:
    idx = pd.date_range("2022-01-01", periods=8, freq="D")
    entry = pd.Series([True, True, False, False, True, True, True, False], index=idx)
    exit_signal = pd.Series([False, True, True, True, False, False, True, True], index=idx)

    state = stateful_regime_signal(entry, exit_signal, min_hold_days=2, max_hold_days=4, cooldown_days=2)

    assert state.tolist() == [True, False, False, False, True, True, False, False]


def test_regime_defense_report_activates_on_2022_like_features() -> None:
    idx = pd.date_range("2021-01-01", periods=300, freq="D")
    close = pd.DataFrame(index=idx)
    close["SPY"] = list(pd.Series(range(300), index=idx).map(lambda x: 500 - x * 0.9))
    close["QQQ"] = list(pd.Series(range(300), index=idx).map(lambda x: 400 - x * 1.0))
    close["TLT"] = list(pd.Series(range(300), index=idx).map(lambda x: 160 - x * 0.25))
    close["UUP"] = list(pd.Series(range(300), index=idx).map(lambda x: 24 + x * 0.03))
    close["^VIX"] = list(pd.Series(range(300), index=idx).map(lambda x: 18 + x * 0.03))
    cfg = {
        "regime_defense_guard": {
            "enabled": True,
            "defense_symbol": "PFIX",
            "defense_exposure": 1.0,
            "entry_signal": "early_score2_macro",
            "exit_signal": "bear_score_low",
            "min_hold_days": 10,
            "max_hold_days": 90,
            "cooldown_days": 10,
        }
    }

    report = regime_defense_report(close, cfg)

    assert report["active"] is True
    assert report["defense_symbol"] == "PFIX"


def test_partial_defense_report_activates_on_breadth_drawdown() -> None:
    idx = pd.date_range("2024-01-01", periods=90, freq="D")
    close = pd.DataFrame(index=idx)
    close["QQQ"] = [100.0] * 87 + [91.0] * 3
    close["AAA"] = [100.0] * 87 + [90.0] * 3
    close["BBB"] = [100.0] * 87 + [89.0] * 3
    close["CCC"] = [100.0] * 87 + [88.0] * 3
    cfg = {
        "partial_defense_guard": {
            "enabled": True,
            "name": "breadth33_qdd8_bil50",
            "defense_symbol": "BIL",
            "defense_weight": 0.5,
            "signal": "breadth33_qdd8",
            "hold_days": 3,
            "cooldown_days": 5,
        }
    }

    report = partial_defense_report(close, cfg, ["AAA", "BBB", "CCC"])

    assert report["active"] is True
    assert report["defense_symbol"] == "BIL"
    assert report["defense_weight"] == 0.5


def test_apply_partial_defense_overlay_scales_targets() -> None:
    adjusted = apply_partial_defense_overlay(
        {"AAA": 0.6, "BBB": 0.6},
        {"active": True, "defense_symbol": "BIL", "defense_weight": 0.5},
    )

    assert adjusted == {"AAA": 0.3, "BBB": 0.3, "BIL": 0.5}


def test_apply_quantum_sleeve_defense_replaces_quantum_when_partial_active() -> None:
    exposures, report = apply_quantum_sleeve_defense(
        {"IONQ": 0.01, "RGTI": 0.01, "QBTS": 0.01, "QUBT": 0.01, "ARQQ": 0.01},
        {"enabled": True, "allocation": 0.05},
        {"active": True},
        {
            "quantum_sleeve": {
                "defense_guard": {
                    "enabled": True,
                    "name": "quantum_to_bil_on_breadth33_qdd8",
                    "defense_symbol": "BIL",
                    "trigger": "partial_defense_active",
                }
            }
        },
    )

    assert exposures == {"BIL": 0.05}
    assert report["defense_guard"]["active"] is True


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
