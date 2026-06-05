from __future__ import annotations

import argparse
import json
import math
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.trading.alpaca_paper import AlpacaPaperTradingClient
from src.utils.io_utils import ensure_dir, load_yaml_config, write_json


DAILY_BUY_BLOCK_LOSS_PCT = 0.02
MIN_DELTA_NOTIONAL = 25.0
MIN_FRACTIONAL_NOTIONAL = 1.0
MIN_MANAGED_POSITION_QTY = 0.000001
BUYING_POWER_BUFFER = 0.98
ORDER_BUYING_POWER_BUFFER = 0.98
MANAGED_ORDER_PREFIX = "gh-puremom"
DEFAULT_STATE_PATH = "state/pure_momentum_state.json"
OPEN_ORDER_STATES = {"new", "accepted", "pending_new", "partially_filled", "pending_replace", "pending_cancel"}
FILLED_ORDER_STATES = {"filled", "partially_filled"}


def format_qty(qty: float) -> str:
    return f"{float(qty):.6f}".rstrip("0").rstrip(".")


@dataclass
class PaperOrderPlan:
    symbol: str
    side: str
    qty: float
    price: float
    target_qty: float
    notional_amount: float | None = None

    @property
    def notional(self) -> float:
        if self.notional_amount is not None:
            return float(self.notional_amount)
        return float(self.qty * self.price)

    def payload(self, run_key: str) -> dict[str, Any]:
        suffix = f"{int(time.time() * 1000)}"
        payload = {
            "symbol": self.symbol,
            "side": self.side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"{MANAGED_ORDER_PREFIX}-{run_key}-{self.symbol.lower()}-{self.side}-{suffix}",
        }
        if self.notional_amount is not None:
            payload["notional"] = f"{self.notional_amount:.2f}"
        else:
            payload["qty"] = format_qty(self.qty)
        return payload

    def resized(self, qty: float) -> "PaperOrderPlan":
        return PaperOrderPlan(
            symbol=self.symbol,
            side=self.side,
            qty=qty,
            price=self.price,
            target_qty=self.target_qty,
        )

    def resized_notional(self, notional_amount: float) -> "PaperOrderPlan":
        return PaperOrderPlan(
            symbol=self.symbol,
            side=self.side,
            qty=0,
            price=self.price,
            target_qty=self.target_qty,
            notional_amount=notional_amount,
        )


@dataclass(frozen=True)
class SelectionResult:
    symbols: list[str]
    mode: str
    breadth20: float | None
    spy20: float | None
    spy_dd63: float | None
    target_gross_override: float | None = None
    target_weights: dict[str, float] | None = None


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def latest_completed_daily_close(raw: pd.DataFrame) -> pd.DataFrame:
    close = raw["Close"] if isinstance(raw.columns, pd.MultiIndex) else raw[["Close"]]
    if isinstance(close, pd.Series):
        close = close.to_frame()
    close.index = pd.to_datetime(close.index).tz_localize(None)

    now_ny = pd.Timestamp.now(tz="America/New_York")
    today_ny = now_ny.tz_localize(None).normalize()
    return close.loc[close.index.normalize() < today_ny].ffill().dropna(how="all")


def fetch_close_frame(symbols: list[str], lookback_days: int) -> pd.DataFrame:
    start = (pd.Timestamp.utcnow().normalize() - pd.Timedelta(days=max(lookback_days * 4, 365))).date().isoformat()
    raw = yf.download(symbols, start=start, auto_adjust=True, progress=False, threads=True)
    if raw.empty:
        raise RuntimeError("No Yahoo daily data downloaded")
    close = latest_completed_daily_close(raw)
    if isinstance(close.columns, pd.MultiIndex):
        close = close.droplevel(0, axis=1)
    close = close[[symbol for symbol in symbols if symbol in close.columns]]
    if len(close) <= lookback_days:
        raise RuntimeError(f"Not enough completed daily bars: {len(close)} <= {lookback_days}")
    return close


def forward_lock_report(strategy_cfg: dict[str, Any], evidence_cfg: dict[str, Any]) -> dict[str, Any]:
    lock_cfg = evidence_cfg.get("forward_lock", {}) or {}
    if not bool(lock_cfg.get("enabled", False)):
        return {"enabled": False, "passed": True, "mismatches": []}

    checks: list[tuple[str, Any, Any]] = [
        ("strategy_name", strategy_cfg.get("name"), lock_cfg.get("strategy_name")),
        ("lookback_days", int(strategy_cfg.get("lookback_days", 0)), int(lock_cfg.get("lookback_days", 0))),
        ("top_n", int(strategy_cfg.get("top_n", 0)), int(lock_cfg.get("top_n", 0))),
        ("rebalance_days", int(strategy_cfg.get("rebalance_days", 0)), int(lock_cfg.get("rebalance_days", 0))),
        (
            "target_gross_leverage",
            round(float(strategy_cfg.get("target_gross_leverage", 0.0)), 6),
            round(float(lock_cfg.get("target_gross_leverage", 0.0)), 6),
        ),
        (
            "max_gross_leverage",
            round(float(strategy_cfg.get("max_gross_leverage", 0.0)), 6),
            round(float(lock_cfg.get("max_gross_leverage", 0.0)), 6),
        ),
    ]
    mismatches = [
        {"field": field, "actual": actual, "locked": locked}
        for field, actual, locked in checks
        if actual != locked
    ]

    actual_caps = {str(k).upper(): round(float(v), 6) for k, v in (strategy_cfg.get("symbol_weight_caps", {}) or {}).items()}
    locked_caps = {str(k).upper(): round(float(v), 6) for k, v in (lock_cfg.get("symbol_weight_caps", {}) or {}).items()}
    if actual_caps != locked_caps:
        mismatches.append({"field": "symbol_weight_caps", "actual": actual_caps, "locked": locked_caps})

    actual_sleeves = {
        str(item.get("name")): round(float(item.get("allocation", 0.0)), 6)
        for item in (strategy_cfg.get("phase_sleeves") or [])
    }
    locked_sleeves = {str(k): round(float(v), 6) for k, v in (lock_cfg.get("phase_sleeves", {}) or {}).items()}
    if actual_sleeves != locked_sleeves:
        mismatches.append({"field": "phase_sleeves", "actual": actual_sleeves, "locked": locked_sleeves})

    quantum_cfg = strategy_cfg.get("quantum_sleeve", {}) or {}
    locked_quantum = lock_cfg.get("quantum_sleeve")
    if locked_quantum is not None:
        actual_quantum = normalize_quantum_sleeve_for_lock(quantum_cfg, locked_quantum)
        expected_quantum = normalize_quantum_sleeve_for_lock(locked_quantum, locked_quantum)
        if actual_quantum != expected_quantum:
            mismatches.append({"field": "quantum_sleeve", "actual": actual_quantum, "locked": expected_quantum})

    defense_cfg = strategy_cfg.get("regime_defense_guard", {}) or {}
    locked_defense = lock_cfg.get("regime_defense_guard")
    if locked_defense is not None:
        actual_defense = normalize_regime_defense_guard_for_lock(defense_cfg)
        expected_defense = normalize_regime_defense_guard_for_lock(locked_defense)
        if actual_defense != expected_defense:
            mismatches.append({"field": "regime_defense_guard", "actual": actual_defense, "locked": expected_defense})

    partial_defense_cfg = strategy_cfg.get("partial_defense_guard", {}) or {}
    locked_partial_defense = lock_cfg.get("partial_defense_guard")
    if locked_partial_defense is not None:
        actual_partial_defense = normalize_partial_defense_guard_for_lock(partial_defense_cfg)
        expected_partial_defense = normalize_partial_defense_guard_for_lock(locked_partial_defense)
        if actual_partial_defense != expected_partial_defense:
            mismatches.append(
                {"field": "partial_defense_guard", "actual": actual_partial_defense, "locked": expected_partial_defense}
            )

    return {
        "enabled": True,
        "passed": not mismatches,
        "locked_at_utc": lock_cfg.get("locked_at_utc"),
        "mismatches": mismatches,
    }


def normalize_regime_defense_guard_for_lock(guard_cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(guard_cfg.get("enabled", False)),
        "name": str(guard_cfg.get("name", "")),
        "defense_symbol": str(guard_cfg.get("defense_symbol", "")).upper(),
        "defense_exposure": round(float(guard_cfg.get("defense_exposure", 0.0)), 6),
        "entry_signal": str(guard_cfg.get("entry_signal", "")),
        "exit_signal": str(guard_cfg.get("exit_signal", "")),
        "min_hold_days": int(guard_cfg.get("min_hold_days", 0)),
        "max_hold_days": int(guard_cfg.get("max_hold_days", 0)),
        "cooldown_days": int(guard_cfg.get("cooldown_days", 0)),
    }


def normalize_quantum_sleeve_for_lock(sleeve_cfg: dict[str, Any], locked_cfg: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {
        "enabled": bool(sleeve_cfg.get("enabled", False)),
        "allocation": round(float(sleeve_cfg.get("allocation", 0.0)), 6),
        "symbols": sorted(str(symbol).upper() for symbol in (sleeve_cfg.get("symbols") or [])),
    }
    if "defense_guard" in locked_cfg:
        guard_cfg = sleeve_cfg.get("defense_guard", {}) or {}
        out["defense_guard"] = {
            "enabled": bool(guard_cfg.get("enabled", False)),
            "name": str(guard_cfg.get("name", "")),
            "defense_symbol": str(guard_cfg.get("defense_symbol", "")).upper(),
            "trigger": str(guard_cfg.get("trigger", "")),
        }
    return out


def normalize_partial_defense_guard_for_lock(guard_cfg: dict[str, Any]) -> dict[str, Any]:
    return {
        "enabled": bool(guard_cfg.get("enabled", False)),
        "name": str(guard_cfg.get("name", "")),
        "defense_symbol": str(guard_cfg.get("defense_symbol", "")).upper(),
        "defense_weight": round(float(guard_cfg.get("defense_weight", 0.0)), 6),
        "signal": str(guard_cfg.get("signal", "")),
        "hold_days": int(guard_cfg.get("hold_days", 0)),
        "cooldown_days": int(guard_cfg.get("cooldown_days", 0)),
    }


def anomaly_guard_report(
    close: pd.DataFrame,
    symbols: list[str],
    evidence_cfg: dict[str, Any],
) -> dict[str, Any]:
    guard_cfg = evidence_cfg.get("anomaly_guard", {}) or {}
    if not bool(guard_cfg.get("enabled", False)):
        return {"enabled": False, "passed": True, "issues": []}

    issues: list[dict[str, Any]] = []
    latest_date = close.index[-1].normalize()
    today_ny = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
    stale_days = int((today_ny - latest_date).days)
    max_stale_days = int(guard_cfg.get("max_completed_data_stale_days", 5))
    if stale_days > max_stale_days:
        issues.append(
            {
                "type": "stale_completed_daily_data",
                "latest_completed_date": latest_date.date().isoformat(),
                "stale_days": stale_days,
                "max_stale_days": max_stale_days,
            }
        )

    latest = close.reindex(columns=symbols).iloc[-1]
    coverage = float(latest.notna().mean()) if len(latest) else 0.0
    min_coverage = float(guard_cfg.get("min_latest_universe_coverage", 0.90))
    if coverage < min_coverage:
        issues.append(
            {
                "type": "low_latest_universe_coverage",
                "coverage": coverage,
                "min_coverage": min_coverage,
                "missing_symbols": [str(symbol) for symbol, value in latest.items() if pd.isna(value)],
            }
        )

    nonpositive = [str(symbol) for symbol, value in latest.items() if pd.notna(value) and float(value) <= 0.0]
    if nonpositive:
        issues.append({"type": "nonpositive_latest_close", "symbols": nonpositive})

    max_abs_daily_return = float(guard_cfg.get("max_abs_daily_return", 0.85))
    if len(close) >= 2:
        last_return = close.reindex(columns=symbols).pct_change().iloc[-1].replace([float("inf"), float("-inf")], pd.NA)
        outliers = {
            str(symbol): float(value)
            for symbol, value in last_return.dropna().items()
            if abs(float(value)) > max_abs_daily_return
        }
        if outliers:
            issues.append(
                {
                    "type": "absurd_latest_daily_return",
                    "max_abs_daily_return": max_abs_daily_return,
                    "symbols": outliers,
                }
            )

    return {
        "enabled": True,
        "passed": not issues,
        "latest_completed_date": latest_date.date().isoformat(),
        "latest_universe_coverage": coverage,
        "issues": issues,
    }


def rank_symbols(
    close: pd.DataFrame,
    symbols: list[str],
    lookback_days: int,
    top_n: int,
    min_momentum: float | None,
    min_short_momentum: float | None = None,
    require_above_ma_days: int | None = None,
) -> list[str]:
    momentum = close[symbols].iloc[-1] / close[symbols].iloc[-lookback_days - 1] - 1.0
    momentum = momentum.replace([float("inf"), float("-inf")], pd.NA).dropna()
    if min_momentum is not None:
        momentum = momentum.loc[momentum >= min_momentum]
    if min_short_momentum is not None:
        short_momentum = close[symbols].iloc[-1] / close[symbols].iloc[-6] - 1.0
        short_momentum = short_momentum.replace([float("inf"), float("-inf")], pd.NA)
        momentum = momentum.loc[short_momentum.reindex(momentum.index) >= min_short_momentum]
    if require_above_ma_days is not None:
        if require_above_ma_days <= 0:
            raise ValueError("require_above_ma_days must be positive")
        if len(close) <= require_above_ma_days:
            return []
        latest = close[symbols].iloc[-1]
        moving_average = close[symbols].iloc[-require_above_ma_days:].mean()
        above_ma = latest > moving_average
        momentum = momentum.loc[above_ma.reindex(momentum.index).fillna(False)]
    momentum = momentum.sort_values(ascending=False)
    return [str(symbol) for symbol in momentum.head(top_n).index]


def rank_symbols_composite_momentum(
    close: pd.DataFrame,
    symbols: list[str],
    top_n: int,
    min_momentum_63d: float | None,
    weights: dict[str, float],
) -> list[str]:
    universe = [symbol for symbol in symbols if symbol in close.columns]
    if len(close) <= 63 or not universe:
        return []

    latest = close[universe].iloc[-1]
    mom63 = latest / close[universe].iloc[-64] - 1.0
    mom20 = latest / close[universe].iloc[-21] - 1.0
    mom5 = latest / close[universe].iloc[-6] - 1.0
    score = (
        float(weights.get("momentum_63", 0.40)) * mom63
        + float(weights.get("momentum_20", 0.40)) * mom20
        + float(weights.get("momentum_5", 0.20)) * mom5
    )
    scores = pd.DataFrame({"score": score, "mom63": mom63}).replace([float("inf"), float("-inf")], pd.NA).dropna()
    if min_momentum_63d is not None:
        scores = scores.loc[scores["mom63"] >= min_momentum_63d]
    scores = scores.sort_values("score", ascending=False)
    return [str(symbol) for symbol in scores.head(top_n).index]


def rank_symbols_spydd_acceleration(
    close: pd.DataFrame,
    symbols: list[str],
    top_n: int,
    min_momentum_63d: float | None,
    min_momentum_20d: float | None,
    min_momentum_5d: float | None,
) -> list[str]:
    universe = [symbol for symbol in symbols if symbol in close.columns]
    if len(close) <= 63 or not universe:
        return []

    latest = close[universe].iloc[-1]
    mom63 = latest / close[universe].iloc[-64] - 1.0
    mom20 = latest / close[universe].iloc[-21] - 1.0
    mom5 = latest / close[universe].iloc[-6] - 1.0
    score = mom20 - mom63
    scores = (
        pd.DataFrame({"score": score, "mom63": mom63, "mom20": mom20, "mom5": mom5})
        .replace([float("inf"), float("-inf")], pd.NA)
        .dropna()
    )
    if min_momentum_63d is not None:
        scores = scores.loc[scores["mom63"] >= min_momentum_63d]
    if min_momentum_20d is not None:
        scores = scores.loc[scores["mom20"] >= min_momentum_20d]
    if min_momentum_5d is not None:
        scores = scores.loc[scores["mom5"] >= min_momentum_5d]
    scores = scores.sort_values("score", ascending=False)
    return [str(symbol) for symbol in scores.head(top_n).index]


def capped_symbol_weights(selected: list[str], caps: dict[str, float]) -> dict[str, float]:
    if not selected:
        return {}
    weights = {symbol: 1.0 / len(selected) for symbol in selected}
    for _ in range(10):
        over = {symbol: weight for symbol, weight in weights.items() if weight > caps.get(symbol, 1.0)}
        if not over:
            break
        excess = 0.0
        for symbol, weight in over.items():
            cap = caps.get(symbol, 1.0)
            excess += weight - cap
            weights[symbol] = cap
        free = [symbol for symbol in selected if symbol not in over]
        free_weight = sum(weights[symbol] for symbol in free)
        if not free or excess <= 0.0 or free_weight <= 0.0:
            break
        for symbol in free:
            weights[symbol] += excess * weights[symbol] / free_weight
    total = sum(weights.values())
    if total <= 0.0:
        return {symbol: 1.0 / len(selected) for symbol in selected}
    return {symbol: weight / total for symbol, weight in weights.items()}


def selection_result(
    symbols: list[str],
    mode: str,
    breadth20: float | None,
    spy20: float | None,
    spy_dd63: float | None,
    strategy_cfg: dict[str, Any],
    target_gross_override: float | None = None,
) -> SelectionResult:
    caps_cfg = strategy_cfg.get("symbol_weight_caps", {}) or {}
    caps = {str(symbol).upper(): float(cap) for symbol, cap in caps_cfg.items()}
    weights = capped_symbol_weights(symbols, caps) if caps else None
    return SelectionResult(symbols, mode, breadth20, spy20, spy_dd63, target_gross_override, weights)


def select_strategy_symbols(close: pd.DataFrame, strategy_cfg: dict[str, Any], universe: list[str]) -> SelectionResult:
    lookback_days = int(strategy_cfg.get("lookback_days", 63))
    top_n = int(strategy_cfg.get("top_n", 7))
    min_momentum_raw = strategy_cfg.get("min_momentum")
    min_momentum = None if min_momentum_raw is None else float(min_momentum_raw)
    min_short_raw = strategy_cfg.get("normal_min_5d_momentum")
    min_short_momentum = None if min_short_raw is None else float(min_short_raw)
    normal_above_ma_days = (
        int(strategy_cfg.get("normal_above_ma_days", 20))
        if bool(strategy_cfg.get("normal_require_above_20d_ma", False))
        else None
    )
    breadth_lookback = int(strategy_cfg.get("breadth_lookback_days", 20))
    mid_low = float(strategy_cfg.get("mid_breadth_cash_low", 0.50))
    mid_high = float(strategy_cfg.get("mid_breadth_cash_high", 0.66))
    mid_sleeve_spy20_min = float(strategy_cfg.get("mid_breadth_sleeve_spy20_min", 0.025))
    mid_sleeve_top_n = int(strategy_cfg.get("mid_breadth_sleeve_top_n", 3))
    mid_sleeve_gross_raw = strategy_cfg.get("mid_breadth_sleeve_gross_leverage")
    mid_sleeve_gross = None if mid_sleeve_gross_raw is None else float(mid_sleeve_gross_raw)
    weak_breadth = float(strategy_cfg.get("weak_breadth_threshold", 0.33))
    weak_breadth_score = str(strategy_cfg.get("weak_breadth_score", "momentum_63")).lower()
    weak_breadth_score_weights = strategy_cfg.get("weak_breadth_score_weights", {})
    spydd_score = str(strategy_cfg.get("spydd_score", "momentum_63")).lower()
    spydd_top_n = int(strategy_cfg.get("spydd_top_n", 5))
    spydd_min_mom20_raw = strategy_cfg.get("spydd_min_20d_momentum")
    spydd_min_mom20 = None if spydd_min_mom20_raw is None else float(spydd_min_mom20_raw)
    spydd_min_mom5_raw = strategy_cfg.get("spydd_min_5d_momentum")
    spydd_min_mom5 = None if spydd_min_mom5_raw is None else float(spydd_min_mom5_raw)
    spy_symbol = str(strategy_cfg.get("regime_symbol", "SPY")).upper()

    if len(close) <= max(lookback_days, 63, breadth_lookback) or spy_symbol not in close.columns:
        return selection_result([], "insufficient_regime_data", None, None, None, strategy_cfg)

    breadth20 = float(((close[universe].iloc[-1] / close[universe].iloc[-breadth_lookback - 1] - 1.0) > 0.0).mean())
    spy = close[spy_symbol]
    spy20 = float(spy.iloc[-1] / spy.iloc[-breadth_lookback - 1] - 1.0)
    spy_dd63 = float(spy.iloc[-1] / spy.iloc[-63:].max() - 1.0)

    if spy20 < -0.05:
        return selection_result(
            rank_symbols(close, universe, 5, 7, min_momentum),
            "loss_spy20_crash_lb5_top7",
            breadth20,
            spy20,
            spy_dd63,
            strategy_cfg,
        )
    if -0.05 <= spy_dd63 <= -0.02:
        if spydd_score == "acceleration_20_minus_63":
            return selection_result(
                rank_symbols_spydd_acceleration(close, universe, spydd_top_n, min_momentum, spydd_min_mom20, spydd_min_mom5),
                "loss_spy_dd_2_5_accel20_minus63_top2",
                breadth20,
                spy20,
                spy_dd63,
                strategy_cfg,
            )
        return selection_result(
            rank_symbols(close, universe, lookback_days, 5, min_momentum),
            "loss_spy_dd_2_5_lb63_top5",
            breadth20,
            spy20,
            spy_dd63,
            strategy_cfg,
        )
    if breadth20 < weak_breadth:
        if weak_breadth_score == "composite_63_20_5":
            return selection_result(
                rank_symbols_composite_momentum(close, universe, 5, min_momentum, weak_breadth_score_weights),
                "loss_weak_breadth_comp_40_40_20_top5",
                breadth20,
                spy20,
                spy_dd63,
                strategy_cfg,
            )
        return selection_result(
            rank_symbols(close, universe, lookback_days, 5, min_momentum),
            "loss_weak_breadth_lb63_top5",
            breadth20,
            spy20,
            spy_dd63,
            strategy_cfg,
        )
    if -0.02 <= spy20 < 0.0:
        return selection_result(
            rank_symbols(close, universe, lookback_days, 5, min_momentum),
            "loss_spy20_mild_neg_lb63_top5",
            breadth20,
            spy20,
            spy_dd63,
            strategy_cfg,
        )
    if mid_low <= breadth20 < mid_high and mid_sleeve_gross is not None and spy20 >= mid_sleeve_spy20_min:
        return selection_result(
            rank_symbols(close, universe, lookback_days, mid_sleeve_top_n, min_momentum),
            f"normal_mid_breadth_spy20_top{mid_sleeve_top_n}_gross{mid_sleeve_gross:g}",
            breadth20,
            spy20,
            spy_dd63,
            strategy_cfg,
            mid_sleeve_gross,
        )
    if mid_low <= breadth20 < mid_high:
        return selection_result([], "normal_mid_breadth_cash", breadth20, spy20, spy_dd63, strategy_cfg)
    return selection_result(
        rank_symbols(close, universe, lookback_days, top_n, min_momentum, min_short_momentum, normal_above_ma_days),
        "normal_lb63_top7_mom5_pos_above20ma" if normal_above_ma_days == 20 else "normal_lb63_top7_mom5_pos",
        breadth20,
        spy20,
        spy_dd63,
        strategy_cfg,
    )


def load_state(path: str | Path) -> dict[str, Any]:
    state_path = ROOT / path
    if not state_path.exists():
        return {}
    return json.loads(state_path.read_text(encoding="utf-8"))


def save_state(path: str | Path, state: dict[str, Any]) -> None:
    state_path = ROOT / path
    state_path.parent.mkdir(parents=True, exist_ok=True)
    state_path.write_text(json.dumps(state, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def trading_days_since(close: pd.DataFrame, last_rebalance_date: str | None) -> int | None:
    if not last_rebalance_date:
        return None
    last = pd.Timestamp(last_rebalance_date).tz_localize(None).normalize()
    completed = close.index.normalize()
    return int((completed > last).sum())


def is_rebalance_due(
    close: pd.DataFrame,
    state: dict[str, Any],
    rebalance_days: int,
    has_managed_position: bool,
) -> tuple[bool, str, int | None]:
    last_rebalance_date = state.get("last_rebalance_date")
    days_since = trading_days_since(close, last_rebalance_date)
    if not has_managed_position:
        return True, "no_managed_position", days_since
    if days_since is None:
        return True, "no_rebalance_state", days_since
    if days_since >= max(rebalance_days, 1):
        return True, "rebalance_interval_due", days_since
    return False, "rebalance_interval_wait", days_since


def regime_defense_symbols(strategy_cfg: dict[str, Any]) -> list[str]:
    guard_cfg = strategy_cfg.get("regime_defense_guard", {}) or {}
    partial_cfg = strategy_cfg.get("partial_defense_guard", {}) or {}
    symbols: list[str] = []
    if not bool(guard_cfg.get("enabled", False)):
        guard_cfg = {}
    if bool(guard_cfg.get("enabled", False)):
        symbols.append(str(guard_cfg.get("defense_symbol", "")).upper())
        symbols.extend(str(symbol).upper() for symbol in (guard_cfg.get("market_symbols") or []))
    if bool(partial_cfg.get("enabled", False)):
        symbols.append(str(partial_cfg.get("defense_symbol", "")).upper())
        symbols.extend(str(symbol).upper() for symbol in (partial_cfg.get("market_symbols") or []))
    return sorted(symbol for symbol in set(symbols) if symbol)


def regime_defense_features(close: pd.DataFrame) -> pd.DataFrame:
    required = ["SPY", "QQQ", "TLT", "UUP"]
    missing = [symbol for symbol in required if symbol not in close.columns]
    if missing:
        raise RuntimeError(f"Regime defense guard missing market data columns: {missing}")
    spy = close["SPY"]
    qqq = close["QQQ"]
    tlt = close["TLT"]
    uup = close["UUP"]
    vix = close["^VIX"] if "^VIX" in close.columns else pd.Series(float("nan"), index=close.index)
    features = pd.DataFrame(index=close.index)
    features["spy_ma100"] = spy.rolling(100).mean()
    features["spy_ma200"] = spy.rolling(200).mean()
    features["qqq_ma50"] = qqq.rolling(50).mean()
    features["qqq_ma100"] = qqq.rolling(100).mean()
    features["qqq_ma200"] = qqq.rolling(200).mean()
    features["spy_ret126"] = spy / spy.shift(126) - 1.0
    features["qqq_ret42"] = qqq / qqq.shift(42) - 1.0
    features["qqq_ret126"] = qqq / qqq.shift(126) - 1.0
    features["tlt_ret63"] = tlt / tlt.shift(63) - 1.0
    features["uup_ret63"] = uup / uup.shift(63) - 1.0
    features["qqq_dd126"] = qqq / qqq.rolling(126).max() - 1.0
    features["qqq_dd252"] = qqq / qqq.rolling(252).max() - 1.0
    features["vix_ma20"] = vix.rolling(20).mean()
    features["spy"] = spy
    features["qqq"] = qqq
    features["vix"] = vix
    features["bear_score"] = (
        (spy < features["spy_ma200"]).astype(int)
        + (qqq < features["qqq_ma200"]).astype(int)
        + (features["spy_ret126"] < -0.08).astype(int)
        + (features["qqq_ret126"] < -0.12).astype(int)
        + (features["qqq_dd252"] < -0.18).astype(int)
        + ((features["tlt_ret63"] < -0.04) & (features["uup_ret63"] > 0.02)).astype(int)
    )
    features["early_score"] = (
        (spy < features["spy_ma100"]).astype(int)
        + (qqq < features["qqq_ma100"]).astype(int)
        + (features["qqq_ret42"] < -0.08).astype(int)
        + (features["qqq_dd126"] < -0.12).astype(int)
        + (vix > features["vix_ma20"] * 1.15).astype(int)
    )
    return features


def regime_defense_entry_signal(features: pd.DataFrame, name: str) -> pd.Series:
    if name == "early_score2_macro":
        return (features["early_score"] >= 2) & (features["tlt_ret63"] < -0.03) & (features["uup_ret63"] > 0.01)
    raise ValueError(f"Unsupported regime defense entry signal: {name}")


def regime_defense_exit_signal(features: pd.DataFrame, name: str) -> pd.Series:
    if name == "bear_score_low":
        return features["bear_score"] <= 1
    raise ValueError(f"Unsupported regime defense exit signal: {name}")


def stateful_regime_signal(
    entry: pd.Series,
    exit_signal: pd.Series,
    min_hold_days: int,
    max_hold_days: int,
    cooldown_days: int,
) -> pd.Series:
    entry = entry.fillna(False).astype(bool)
    exit_signal = exit_signal.fillna(False).astype(bool)
    active_values: list[bool] = []
    in_regime = False
    hold_days = 0
    cooldown = 0
    for date in entry.index:
        if cooldown > 0:
            cooldown -= 1
        if in_regime:
            hold_days += 1
            should_exit = hold_days >= min_hold_days and bool(exit_signal.loc[date])
            if max_hold_days > 0 and hold_days >= max_hold_days:
                should_exit = True
            if should_exit:
                in_regime = False
                hold_days = 0
                cooldown = max(cooldown_days, 0)
        elif cooldown == 0 and bool(entry.loc[date]):
            in_regime = True
            hold_days = 1
        active_values.append(in_regime)
    return pd.Series(active_values, index=entry.index)


def regime_defense_report(close: pd.DataFrame, strategy_cfg: dict[str, Any]) -> dict[str, Any]:
    guard_cfg = strategy_cfg.get("regime_defense_guard", {}) or {}
    if not bool(guard_cfg.get("enabled", False)):
        return {"enabled": False, "active": False}
    features = regime_defense_features(close)
    entry_name = str(guard_cfg.get("entry_signal", "early_score2_macro"))
    exit_name = str(guard_cfg.get("exit_signal", "bear_score_low"))
    entry = regime_defense_entry_signal(features, entry_name)
    exit_signal = regime_defense_exit_signal(features, exit_name)
    min_hold_days = int(guard_cfg.get("min_hold_days", 10))
    max_hold_days = int(guard_cfg.get("max_hold_days", 90))
    cooldown_days = int(guard_cfg.get("cooldown_days", 10))
    state = stateful_regime_signal(entry, exit_signal, min_hold_days, max_hold_days, cooldown_days)
    latest = features.iloc[-1]
    active = bool(state.iloc[-1])
    active_prev = bool(state.iloc[-2]) if len(state) > 1 else False
    return {
        "enabled": True,
        "name": str(guard_cfg.get("name", "stateful_2022_pfix_guard")),
        "active": active,
        "active_previous_completed_day": active_prev,
        "latest_completed_date": close.index[-1].date().isoformat(),
        "defense_symbol": str(guard_cfg.get("defense_symbol", "PFIX")).upper(),
        "defense_exposure": float(guard_cfg.get("defense_exposure", 1.0)),
        "entry_signal": entry_name,
        "exit_signal": exit_name,
        "entry_now": bool(entry.iloc[-1]),
        "exit_now": bool(exit_signal.iloc[-1]),
        "min_hold_days": min_hold_days,
        "max_hold_days": max_hold_days,
        "cooldown_days": cooldown_days,
        "early_score": int(latest["early_score"]) if pd.notna(latest["early_score"]) else None,
        "bear_score": int(latest["bear_score"]) if pd.notna(latest["bear_score"]) else None,
        "tlt_ret63": as_float(latest.get("tlt_ret63"), float("nan")),
        "uup_ret63": as_float(latest.get("uup_ret63"), float("nan")),
        "qqq_ret42": as_float(latest.get("qqq_ret42"), float("nan")),
        "qqq_dd126": as_float(latest.get("qqq_dd126"), float("nan")),
    }


def partial_defense_features(close: pd.DataFrame, symbols: list[str]) -> pd.DataFrame:
    required = ["QQQ"]
    missing = [symbol for symbol in required if symbol not in close.columns]
    if missing:
        raise RuntimeError(f"Partial defense guard missing market data columns: {missing}")
    qqq = close["QQQ"]
    features = pd.DataFrame(index=close.index)
    available = [symbol for symbol in symbols if symbol in close.columns]
    if available:
        breadth = close[available] / close[available].shift(20) - 1.0
        features["breadth20"] = (breadth > 0.0).mean(axis=1)
    else:
        features["breadth20"] = float("nan")
    features["qqq_dd63"] = qqq / qqq.rolling(63).max() - 1.0
    return features


def partial_defense_signal(features: pd.DataFrame, name: str) -> pd.Series:
    if name == "breadth33_qdd8":
        return (features["breadth20"] < 0.33) & (features["qqq_dd63"] < -0.08)
    raise ValueError(f"Unsupported partial defense signal: {name}")


def fixed_hold_signal(entry: pd.Series, hold_days: int, cooldown_days: int) -> pd.Series:
    entry = entry.fillna(False).astype(bool)
    active_values: list[bool] = []
    hold_left = 0
    cooldown = 0
    for date in entry.index:
        if cooldown > 0:
            cooldown -= 1
        if hold_left > 0:
            active_values.append(True)
            hold_left -= 1
            if hold_left == 0:
                cooldown = max(cooldown_days, 0)
        elif cooldown == 0 and bool(entry.loc[date]):
            active_values.append(True)
            hold_left = max(hold_days - 1, 0)
        else:
            active_values.append(False)
    return pd.Series(active_values, index=entry.index)


def partial_defense_report(close: pd.DataFrame, strategy_cfg: dict[str, Any], symbols: list[str]) -> dict[str, Any]:
    guard_cfg = strategy_cfg.get("partial_defense_guard", {}) or {}
    if not bool(guard_cfg.get("enabled", False)):
        return {"enabled": False, "active": False}
    features = partial_defense_features(close, symbols)
    signal_name = str(guard_cfg.get("signal", "breadth33_qdd8"))
    entry = partial_defense_signal(features, signal_name)
    hold_days = int(guard_cfg.get("hold_days", 3))
    cooldown_days = int(guard_cfg.get("cooldown_days", 5))
    state = fixed_hold_signal(entry, hold_days, cooldown_days)
    latest = features.iloc[-1]
    active = bool(state.iloc[-1])
    active_prev = bool(state.iloc[-2]) if len(state) > 1 else False
    return {
        "enabled": True,
        "name": str(guard_cfg.get("name", "breadth33_qdd8_bil50")),
        "active": active,
        "active_previous_completed_day": active_prev,
        "latest_completed_date": close.index[-1].date().isoformat(),
        "defense_symbol": str(guard_cfg.get("defense_symbol", "BIL")).upper(),
        "defense_weight": float(guard_cfg.get("defense_weight", 0.5)),
        "signal": signal_name,
        "entry_now": bool(entry.iloc[-1]),
        "hold_days": hold_days,
        "cooldown_days": cooldown_days,
        "breadth20": as_float(latest.get("breadth20"), float("nan")),
        "qqq_dd63": as_float(latest.get("qqq_dd63"), float("nan")),
    }


def apply_partial_defense_overlay(
    target_exposures: dict[str, float],
    partial_guard: dict[str, Any],
) -> dict[str, float]:
    if not partial_guard.get("active"):
        return target_exposures
    defense_symbol = str(partial_guard.get("defense_symbol", "")).upper()
    defense_weight = min(max(float(partial_guard.get("defense_weight", 0.0)), 0.0), 1.0)
    if not defense_symbol or defense_weight <= 0.0:
        return target_exposures
    scaled = {symbol: float(exposure) * (1.0 - defense_weight) for symbol, exposure in target_exposures.items()}
    scaled[defense_symbol] = scaled.get(defense_symbol, 0.0) + defense_weight
    return {symbol: exposure for symbol, exposure in scaled.items() if abs(exposure) > 1e-12}


def rebalance_phase_status(
    state: dict[str, Any],
    trading_days_elapsed: int | None,
    rebalance_days: int,
    phase_lock_enabled: bool,
    phase_anchor_date: str | None = None,
) -> dict[str, Any]:
    if not phase_lock_enabled:
        return {"enabled": False, "aligned": True, "phase_offset": None}
    if trading_days_elapsed is None:
        return {"enabled": True, "aligned": False, "phase_offset": None}
    interval = max(rebalance_days, 1)
    phase_offset = int(trading_days_elapsed % interval)
    return {
        "enabled": True,
        "aligned": phase_offset == 0,
        "phase_offset": phase_offset,
        "anchor_date": phase_anchor_date or state.get("last_rebalance_date"),
        "interval_days": interval,
    }


def latest_completed_phase_date(
    close: pd.DataFrame,
    phase_anchor_date: str | None,
    rebalance_days: int,
    phase_offset: int,
) -> str | None:
    if not phase_anchor_date:
        return None
    anchor = pd.Timestamp(phase_anchor_date).tz_localize(None).normalize()
    completed = close.index.normalize()
    eligible = completed[completed >= anchor]
    if len(eligible) == 0:
        return None
    interval = max(rebalance_days, 1)
    anchor_index = int((completed < anchor).sum())
    for date in reversed(eligible):
        date_index = int((completed <= date).sum() - 1)
        elapsed = date_index - anchor_index
        if elapsed >= 0 and elapsed % interval == phase_offset:
            return date.date().isoformat()
    return None


def close_through_date(close: pd.DataFrame, date_str: str | None) -> pd.DataFrame:
    if not date_str:
        return close
    date = pd.Timestamp(date_str).tz_localize(None).normalize()
    return close.loc[close.index.normalize() <= date]


def managed_positions(positions: list[dict[str, Any]], universe: set[str]) -> dict[str, float]:
    out: dict[str, float] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        if symbol not in universe:
            continue
        qty = float(position.get("qty", 0.0))
        if abs(qty) < MIN_MANAGED_POSITION_QTY:
            continue
        market_value = position.get("market_value")
        if market_value is not None and abs(float(market_value or 0.0)) < MIN_FRACTIONAL_NOTIONAL:
            continue
        out[symbol] = qty
    return out


def has_blocking_open_order(open_orders: list[dict[str, Any]], universe: set[str]) -> bool:
    for order in open_orders:
        symbol = str(order.get("symbol", "")).upper()
        if symbol in universe:
            return True
        for leg in order.get("legs") or []:
            if str(leg.get("symbol", "")).upper() in universe:
                return True
    return False


def should_block_new_buys(account: dict[str, Any]) -> bool:
    equity = float(account.get("equity") or 0.0)
    last_equity = float(account.get("last_equity") or 0.0)
    return last_equity > 0.0 and equity < last_equity * (1.0 - DAILY_BUY_BLOCK_LOSS_PCT)


def build_plan(
    close: pd.DataFrame,
    selected: list[str],
    current_qty: dict[str, float],
    equity: float,
    target_gross: float,
    max_gross: float,
    target_weights: dict[str, float] | None = None,
) -> list[PaperOrderPlan]:
    target_gross = min(target_gross, max_gross)
    if target_weights is None:
        target_weights = {symbol: 1.0 / len(selected) for symbol in selected} if selected else {}
    target_qty: dict[str, float] = {}
    for symbol in selected:
        price = float(close[symbol].iloc[-1])
        target_weight = target_gross * float(target_weights.get(symbol, 0.0))
        target_qty[symbol] = (equity * target_weight) / price if price > 0 else 0.0

    plans: list[PaperOrderPlan] = []
    all_symbols = sorted(set(current_qty) | set(target_qty))
    for symbol in all_symbols:
        price = float(close[symbol].iloc[-1]) if symbol in close.columns else 0.0
        if price <= 0.0:
            continue
        desired = float(target_qty.get(symbol, 0.0))
        current = float(current_qty.get(symbol, 0.0))
        delta = desired - current
        delta_notional = abs(delta * price)
        if delta_notional < MIN_FRACTIONAL_NOTIONAL:
            continue
        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        if side == "sell":
            qty = min(qty, max(current, 0.0))
            if qty * price < MIN_FRACTIONAL_NOTIONAL:
                continue
            plans.append(PaperOrderPlan(symbol=symbol, side=side, qty=qty, price=price, target_qty=desired))
            continue
        plans.append(
            PaperOrderPlan(
                symbol=symbol,
                side=side,
                qty=0.0,
                price=price,
                target_qty=desired,
                notional_amount=delta_notional,
            )
        )
    return sorted(plans, key=lambda plan: 0 if plan.side == "sell" else 1)


def build_plan_from_target_exposures(
    close: pd.DataFrame,
    target_exposures: dict[str, float],
    current_qty: dict[str, float],
    equity: float,
    max_gross: float,
) -> list[PaperOrderPlan]:
    total_gross = sum(max(0.0, float(weight)) for weight in target_exposures.values())
    scale = min(1.0, max_gross / total_gross) if total_gross > max_gross and total_gross > 0.0 else 1.0
    target_qty: dict[str, float] = {}
    for symbol, exposure in target_exposures.items():
        if symbol not in close.columns:
            continue
        price = float(close[symbol].iloc[-1])
        target_qty[symbol] = (equity * float(exposure) * scale) / price if price > 0 else 0.0

    plans: list[PaperOrderPlan] = []
    all_symbols = sorted(set(current_qty) | set(target_qty))
    for symbol in all_symbols:
        price = float(close[symbol].iloc[-1]) if symbol in close.columns else 0.0
        if price <= 0.0:
            continue
        desired = float(target_qty.get(symbol, 0.0))
        current = float(current_qty.get(symbol, 0.0))
        delta = desired - current
        delta_notional = abs(delta * price)
        if delta_notional < MIN_FRACTIONAL_NOTIONAL:
            continue
        side = "buy" if delta > 0 else "sell"
        qty = abs(delta)
        if side == "sell":
            qty = min(qty, max(current, 0.0))
            if qty * price < MIN_FRACTIONAL_NOTIONAL:
                continue
            plans.append(PaperOrderPlan(symbol=symbol, side=side, qty=qty, price=price, target_qty=desired))
            continue
        plans.append(
            PaperOrderPlan(
                symbol=symbol,
                side=side,
                qty=0.0,
                price=price,
                target_qty=desired,
                notional_amount=delta_notional,
            )
        )
    return sorted(plans, key=lambda plan: 0 if plan.side == "sell" else 1)


def target_qty_from_exposures(
    close: pd.DataFrame,
    target_exposures: dict[str, float],
    equity: float,
    max_gross: float,
) -> dict[str, float]:
    total_gross = sum(max(0.0, float(weight)) for weight in target_exposures.values())
    scale = min(1.0, max_gross / total_gross) if total_gross > max_gross and total_gross > 0.0 else 1.0
    target_qty: dict[str, float] = {}
    for symbol, exposure in target_exposures.items():
        if symbol not in close.columns:
            continue
        price = float(close[symbol].iloc[-1])
        target_qty[symbol] = (equity * float(exposure) * scale) / price if price > 0.0 else 0.0
    return target_qty


def target_exposures_from_selection(
    selected: list[str],
    target_gross: float,
    target_weights: dict[str, float] | None,
) -> dict[str, float]:
    if not selected:
        return {}
    if target_weights is None:
        target_weights = {symbol: 1.0 / len(selected) for symbol in selected}
    return {symbol: target_gross * float(target_weights.get(symbol, 0.0)) for symbol in selected}


def quantum_sleeve_symbols(strategy_cfg: dict[str, Any]) -> list[str]:
    sleeve_cfg = strategy_cfg.get("quantum_sleeve", {}) or {}
    if not bool(sleeve_cfg.get("enabled", False)):
        return []
    return [str(symbol).upper() for symbol in (sleeve_cfg.get("symbols") or [])]


def quantum_sleeve_targets(
    close: pd.DataFrame,
    strategy_cfg: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    sleeve_cfg = strategy_cfg.get("quantum_sleeve", {}) or {}
    if not bool(sleeve_cfg.get("enabled", False)):
        return {}, {"enabled": False}

    allocation = float(sleeve_cfg.get("allocation", 0.0))
    symbols = [symbol for symbol in quantum_sleeve_symbols(strategy_cfg) if symbol in close.columns]
    mode = str(sleeve_cfg.get("mode", "equal_weight_static"))
    if allocation <= 0.0 or not symbols:
        return (
            {},
            {
                "enabled": True,
                "allocation": allocation,
                "symbols": symbols,
                "mode": mode,
                "target_weights": {},
            },
        )

    weight = 1.0 / len(symbols)
    exposures = {symbol: allocation * weight for symbol in symbols}
    return (
        exposures,
        {
            "enabled": True,
            "allocation": allocation,
            "symbols": symbols,
            "mode": mode,
            "target_weights": {symbol: weight for symbol in symbols},
        },
    )


def apply_quantum_sleeve_defense(
    quantum_exposures: dict[str, float],
    quantum_report: dict[str, Any],
    partial_guard: dict[str, Any],
    strategy_cfg: dict[str, Any],
) -> tuple[dict[str, float], dict[str, Any]]:
    sleeve_cfg = strategy_cfg.get("quantum_sleeve", {}) or {}
    guard_cfg = sleeve_cfg.get("defense_guard", {}) or {}
    if not bool(guard_cfg.get("enabled", False)) or not quantum_exposures:
        quantum_report["defense_guard"] = {"enabled": bool(guard_cfg.get("enabled", False)), "active": False}
        return quantum_exposures, quantum_report
    trigger = str(guard_cfg.get("trigger", "partial_defense_active"))
    active = trigger == "partial_defense_active" and bool(partial_guard.get("active"))
    defense_symbol = str(guard_cfg.get("defense_symbol", "BIL")).upper()
    quantum_report["defense_guard"] = {
        "enabled": True,
        "active": active,
        "name": str(guard_cfg.get("name", "quantum_to_bil_on_breadth33_qdd8")),
        "trigger": trigger,
        "defense_symbol": defense_symbol,
    }
    if not active:
        return quantum_exposures, quantum_report
    allocation = sum(float(exposure) for exposure in quantum_exposures.values())
    quantum_report["defense_guard"]["replaced_symbols"] = sorted(quantum_exposures)
    quantum_report["defense_guard"]["replaced_allocation"] = allocation
    return {defense_symbol: allocation}, quantum_report


def combined_phase_sleeve_targets(
    close: pd.DataFrame,
    strategy_cfg: dict[str, Any],
    symbols: list[str],
    state: dict[str, Any],
    phase_anchor_date: str | None,
    rebalance_days: int,
    target_gross: float,
    current_phase_offset: int | None,
    force_rebalance: bool,
) -> tuple[dict[str, float], list[str], list[dict[str, Any]], bool]:
    sleeves_cfg = strategy_cfg.get("phase_sleeves") or []
    if not sleeves_cfg:
        return {}, [], [], False

    sleeve_state = dict(state.get("phase_sleeves", {}))
    target_exposures: dict[str, float] = {}
    selected_all: list[str] = []
    reports: list[dict[str, Any]] = []
    updated = False

    for raw_sleeve in sleeves_cfg:
        name = str(raw_sleeve.get("name", f"phase_{raw_sleeve.get('phase_offset', 0)}"))
        allocation = float(raw_sleeve.get("allocation", 0.0))
        phase_offset = int(raw_sleeve.get("phase_offset", 0))
        latest_phase_date = latest_completed_phase_date(close, phase_anchor_date, rebalance_days, phase_offset)
        stored = dict(sleeve_state.get(name, {}))
        missing_state = not stored.get("last_rebalance_date")
        is_due = force_rebalance or missing_state or stored.get("last_rebalance_date") != latest_phase_date
        if current_phase_offset is not None and not force_rebalance:
            is_due = is_due and (missing_state or current_phase_offset == phase_offset)
        if is_due and latest_phase_date:
            sleeve_close = close_through_date(close, latest_phase_date)
            selection = select_strategy_symbols(sleeve_close, strategy_cfg, symbols)
            stored = {
                "last_rebalance_date": latest_phase_date,
                "selected_symbols": selection.symbols,
                "selection_mode": selection.mode,
                "target_gross_override": selection.target_gross_override,
                "target_weights": selection.target_weights,
                "breadth20": selection.breadth20,
                "spy20": selection.spy20,
                "spy_dd63": selection.spy_dd63,
            }
            sleeve_state[name] = stored
            updated = True

        selected = [str(symbol).upper() for symbol in stored.get("selected_symbols", [])]
        sleeve_gross = float(stored.get("target_gross_override") or target_gross)
        weights = stored.get("target_weights") or capped_symbol_weights(
            selected,
            {str(k).upper(): float(v) for k, v in (strategy_cfg.get("symbol_weight_caps", {}) or {}).items()},
        )
        for symbol in selected:
            selected_all.append(symbol)
            target_exposures[symbol] = target_exposures.get(symbol, 0.0) + allocation * sleeve_gross * float(weights.get(symbol, 0.0))
        reports.append(
            {
                "name": name,
                "allocation": allocation,
                "phase_offset": phase_offset,
                "phase_date": latest_phase_date,
                "due": bool(is_due),
                "selected_symbols": selected,
                "selection_mode": stored.get("selection_mode"),
                "target_gross": sleeve_gross,
                "target_weights": weights,
            }
        )

    if updated:
        state["phase_sleeves"] = sleeve_state
    return target_exposures, sorted(set(selected_all)), reports, updated


def build_liquidation_plan(close: pd.DataFrame, current_qty: dict[str, float]) -> list[PaperOrderPlan]:
    plans: list[PaperOrderPlan] = []
    for symbol in sorted(current_qty):
        qty = float(current_qty[symbol])
        price = float(close[symbol].iloc[-1]) if symbol in close.columns else 0.0
        if price <= 0.0 or qty == 0.0:
            continue
        side = "sell" if qty > 0 else "buy"
        order_qty = abs(qty)
        if order_qty * price < MIN_FRACTIONAL_NOTIONAL:
            continue
        plans.append(PaperOrderPlan(symbol=symbol, side=side, qty=order_qty, price=price, target_qty=0.0))
    return plans


def wait_for_flatten(
    client: AlpacaPaperTradingClient,
    universe: set[str],
    timeout_seconds: int,
) -> tuple[dict[str, float], list[dict[str, Any]]]:
    deadline = time.monotonic() + max(timeout_seconds, 0)
    positions = managed_positions(client.get_positions(), universe)
    open_orders = client.get_orders(status="open", nested=True)
    while (positions or has_blocking_open_order(open_orders, universe)) and time.monotonic() < deadline:
        time.sleep(2.0)
        positions = managed_positions(client.get_positions(), universe)
        open_orders = client.get_orders(status="open", nested=True)
    return positions, open_orders


def fit_buys_to_buying_power(
    plans: list[PaperOrderPlan],
    buying_power: float,
    buying_power_buffer: float = BUYING_POWER_BUFFER,
) -> tuple[list[PaperOrderPlan], dict[str, Any]]:
    buys = [plan for plan in plans if plan.side == "buy"]
    buy_notional = sum(plan.notional for plan in buys)
    buying_power_buffer = min(max(float(buying_power_buffer), 0.0), 1.0)
    available = max(0.0, buying_power * buying_power_buffer)
    info = {
        "applied": False,
        "original_buy_notional": round(buy_notional, 2),
        "available_buy_notional": round(available, 2),
        "buying_power_buffer": buying_power_buffer,
        "scale": 1.0,
        "dropped_buy_symbols": [],
    }
    if buy_notional <= available or buy_notional <= 0.0:
        return plans, info

    scale = available / buy_notional if buy_notional > 0.0 else 0.0
    info["applied"] = True
    info["scale"] = round(scale, 6)
    adjusted: list[PaperOrderPlan] = []
    dropped: list[str] = []
    for plan in plans:
        if plan.side != "buy":
            adjusted.append(plan)
            continue
        if plan.notional_amount is not None:
            scaled_notional = plan.notional * scale
            if scaled_notional < MIN_FRACTIONAL_NOTIONAL:
                dropped.append(plan.symbol)
                continue
            adjusted.append(plan.resized_notional(scaled_notional))
            continue
        qty = int(math.floor(plan.qty * scale))
        if qty < 1 or qty * plan.price < MIN_DELTA_NOTIONAL:
            dropped.append(plan.symbol)
            continue
        adjusted.append(
            PaperOrderPlan(
                symbol=plan.symbol,
                side=plan.side,
                qty=qty,
                price=plan.price,
                target_qty=plan.target_qty,
            )
        )
    info["dropped_buy_symbols"] = dropped
    return adjusted, info


def split_buy_plans_into_rounds(plans: list[PaperOrderPlan], rounds: int) -> list[list[PaperOrderPlan]]:
    rounds = max(1, rounds)
    buys = [plan for plan in plans if plan.side == "buy"]
    notional_buys = [plan for plan in buys if plan.notional_amount is not None]
    share_buys = [plan for plan in buys if plan.notional_amount is None]
    batches: list[list[PaperOrderPlan]] = []
    for plan in notional_buys:
        split_count = min(rounds, max(1, int(math.floor(plan.notional / MIN_FRACTIONAL_NOTIONAL))))
        amount = plan.notional / split_count
        for round_idx in range(split_count):
            while len(batches) <= round_idx:
                batches.append([])
            batches[round_idx].append(plan.resized_notional(amount))

    allocated = {plan.symbol: 0.0 for plan in share_buys}
    for round_idx in range(1, rounds + 1):
        batch: list[PaperOrderPlan] = []
        for plan in share_buys:
            cumulative = int(math.floor(plan.qty * round_idx / rounds))
            qty = cumulative - allocated[plan.symbol]
            if qty < 1 or qty * plan.price < MIN_DELTA_NOTIONAL:
                continue
            allocated[plan.symbol] += qty
            batch.append(plan.resized(qty))
        if batch:
            while len(batches) < round_idx:
                batches.append([])
            batches[round_idx - 1].extend(batch)
    batches = [batch for batch in batches if batch]
    return batches


def buying_power_from_error(exc: requests.HTTPError) -> float | None:
    text = str(exc)
    matches = [
        re.search(r'"daytrading_buying_power"\s*:\s*"([^"]+)"', text),
        re.search(r'"buying_power"\s*:\s*"([^"]+)"', text),
    ]
    match = next((item for item in matches if item), None)
    if not match:
        return None
    try:
        return float(match.group(1))
    except ValueError:
        return None


def cap_buy_to_current_buying_power(
    client: AlpacaPaperTradingClient,
    plan: PaperOrderPlan,
    remaining_buy_count: int = 1,
    order_buying_power_buffer: float = ORDER_BUYING_POWER_BUFFER,
) -> tuple[PaperOrderPlan | None, dict[str, Any]]:
    if plan.side != "buy":
        return plan, {"applied": False}

    account = client.get_account()
    buying_power = float(account.get("buying_power") or 0.0)
    daytrading_buying_power_raw = account.get("daytrading_buying_power")
    daytrading_buying_power = (
        float(daytrading_buying_power_raw)
        if daytrading_buying_power_raw not in (None, "")
        else buying_power
    )
    effective_buying_power = min(buying_power, daytrading_buying_power) if daytrading_buying_power > 0.0 else buying_power
    remaining_buy_count = max(1, remaining_buy_count)
    order_buying_power_buffer = min(max(float(order_buying_power_buffer), 0.0), 1.0)
    notional_cap = (effective_buying_power * order_buying_power_buffer) / remaining_buy_count
    if plan.notional_amount is not None:
        capped_notional = min(plan.notional, notional_cap)
        info = {
            "applied": capped_notional < plan.notional,
            "buying_power": round(buying_power, 2),
            "daytrading_buying_power": round(daytrading_buying_power, 2),
            "effective_buying_power": round(effective_buying_power, 2),
            "order_buying_power_buffer": order_buying_power_buffer,
            "original_notional": round(plan.notional, 2),
            "capped_notional": round(capped_notional, 2),
        }
        if capped_notional < MIN_FRACTIONAL_NOTIONAL:
            return None, info
        return plan.resized_notional(capped_notional), info
    affordable_qty = int(math.floor(notional_cap / plan.price)) if plan.price > 0 else 0
    capped_qty = min(plan.qty, affordable_qty)
    info = {
        "applied": capped_qty < plan.qty,
        "buying_power": round(buying_power, 2),
        "daytrading_buying_power": round(daytrading_buying_power, 2),
        "effective_buying_power": round(effective_buying_power, 2),
        "order_buying_power_buffer": order_buying_power_buffer,
        "original_qty": plan.qty,
        "capped_qty": capped_qty,
    }
    if capped_qty < 1 or capped_qty * plan.price < MIN_DELTA_NOTIONAL:
        fractional_notional = min(plan.notional, notional_cap)
        if fractional_notional >= MIN_FRACTIONAL_NOTIONAL:
            info["fractional_notional"] = round(fractional_notional, 2)
            return plan.resized_notional(fractional_notional), info
        return None, info
    return plan.resized(capped_qty), info


def wait_for_order_done(
    client: AlpacaPaperTradingClient,
    order_id: str | None,
    timeout_seconds: int,
) -> dict[str, Any] | None:
    if not order_id or timeout_seconds <= 0:
        return None
    deadline = time.monotonic() + timeout_seconds
    last_order: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last_order = client.get_order(order_id)
        status = str(last_order.get("status", "")).lower()
        if status not in OPEN_ORDER_STATES:
            return last_order
        time.sleep(2.0)
    return last_order


def submit_plans(
    client: AlpacaPaperTradingClient,
    plans: list[PaperOrderPlan],
    execute: bool,
    run_key: str,
    wait_after_order_seconds: int = 0,
    order_buying_power_buffer: float = ORDER_BUYING_POWER_BUFFER,
) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    for plan_idx, plan in enumerate(plans):
        cap_info: dict[str, Any] = {"applied": False}
        if execute:
            remaining_buy_count = sum(1 for item in plans[plan_idx:] if item.side == "buy")
            capped_plan, cap_info = cap_buy_to_current_buying_power(
                client,
                plan,
                remaining_buy_count,
                order_buying_power_buffer=order_buying_power_buffer,
            )
            if capped_plan is None:
                submitted.append(
                    {
                        "symbol": plan.symbol,
                        "side": plan.side,
                        "qty": plan.qty,
                        "notional_estimate": round(plan.notional, 2),
                        "target_qty": plan.target_qty,
                        "state": "skipped_insufficient_buying_power",
                        "buying_power_cap": cap_info,
                    }
                )
                continue
            plan = capped_plan

        payload = plan.payload(run_key)
        row = {
            "symbol": plan.symbol,
            "side": plan.side,
            "qty": plan.qty,
            "notional_estimate": round(plan.notional, 2),
            "target_qty": plan.target_qty,
            "client_order_id": payload["client_order_id"],
            "state": "planned",
        }
        if cap_info.get("applied"):
            row["buying_power_cap"] = cap_info
        if execute:
            try:
                result = client.submit_order(payload)
                row["state"] = result.get("status", "submitted")
                row["alpaca_order_id"] = result.get("id")
                final_order = wait_for_order_done(client, row.get("alpaca_order_id"), wait_after_order_seconds)
                if final_order:
                    row["state"] = final_order.get("status", row["state"])
                    row["filled_qty"] = final_order.get("filled_qty")
                    row["filled_avg_price"] = final_order.get("filled_avg_price")
            except requests.HTTPError as exc:
                buying_power = buying_power_from_error(exc)
                retry_notional = buying_power * order_buying_power_buffer if buying_power else 0.0
                retry_qty = int(math.floor(retry_notional / plan.price)) if buying_power and plan.notional_amount is None else 0
                retry_qty = min(max(plan.qty - 1, 0), retry_qty)
                if plan.side == "buy" and retry_qty >= 1 and retry_qty * plan.price >= MIN_DELTA_NOTIONAL:
                    retry_plan = plan.resized(retry_qty)
                    retry_payload = retry_plan.payload(run_key)
                    try:
                        result = client.submit_order(retry_payload)
                        row.update(
                            {
                                "qty": retry_plan.qty,
                                "notional_estimate": round(retry_plan.notional, 2),
                                "client_order_id": retry_payload["client_order_id"],
                                "state": result.get("status", "submitted"),
                                "alpaca_order_id": result.get("id"),
                                "buying_power_retry": {
                                    "applied": True,
                                    "buying_power": buying_power,
                                    "original_qty": plan.qty,
                                    "retry_qty": retry_plan.qty,
                                },
                            }
                        )
                        final_order = wait_for_order_done(client, row.get("alpaca_order_id"), wait_after_order_seconds)
                        if final_order:
                            row["state"] = final_order.get("status", row["state"])
                            row["filled_qty"] = final_order.get("filled_qty")
                            row["filled_avg_price"] = final_order.get("filled_avg_price")
                    except requests.HTTPError as retry_exc:
                        row["state"] = "rejected_insufficient_buying_power"
                        row["buying_power_retry"] = {
                            "applied": True,
                            "buying_power": buying_power,
                            "original_qty": plan.qty,
                            "retry_qty": retry_plan.qty,
                        }
                        row["error"] = str(retry_exc)
                elif plan.side == "buy" and retry_notional >= MIN_FRACTIONAL_NOTIONAL:
                    retry_plan = plan.resized_notional(min(plan.notional, retry_notional))
                    retry_payload = retry_plan.payload(run_key)
                    try:
                        result = client.submit_order(retry_payload)
                        row.update(
                            {
                                "qty": retry_plan.qty,
                                "notional_estimate": round(retry_plan.notional, 2),
                                "client_order_id": retry_payload["client_order_id"],
                                "state": result.get("status", "submitted"),
                                "alpaca_order_id": result.get("id"),
                                "buying_power_retry": {
                                    "applied": True,
                                    "buying_power": buying_power,
                                    "original_qty": plan.qty,
                                    "retry_notional": retry_plan.notional,
                                },
                            }
                        )
                        final_order = wait_for_order_done(client, row.get("alpaca_order_id"), wait_after_order_seconds)
                        if final_order:
                            row["state"] = final_order.get("status", row["state"])
                            row["filled_qty"] = final_order.get("filled_qty")
                            row["filled_avg_price"] = final_order.get("filled_avg_price")
                    except requests.HTTPError as retry_exc:
                        row["state"] = "rejected_insufficient_buying_power"
                        row["buying_power_retry"] = {
                            "applied": True,
                            "buying_power": buying_power,
                            "original_qty": plan.qty,
                            "retry_notional": retry_plan.notional,
                        }
                        row["error"] = str(retry_exc)
                else:
                    row["state"] = "rejected_insufficient_buying_power" if buying_power is not None else "rejected_order_error"
                    row["error"] = str(exc)
            time.sleep(0.5)
        submitted.append(row)
    return submitted


def submit_rebalance_plans(
    client: AlpacaPaperTradingClient,
    plans: list[PaperOrderPlan],
    execute: bool,
    run_key: str,
    wait_after_order_seconds: int,
    buy_submission_rounds: int,
    order_buying_power_buffer: float = ORDER_BUYING_POWER_BUFFER,
) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    sell_plans = [plan for plan in plans if plan.side == "sell"]
    buy_plans = [plan for plan in plans if plan.side == "buy"]
    if sell_plans:
        submitted.extend(
            submit_plans(
                client,
                sell_plans,
                execute,
                f"{run_key}-sell",
                wait_after_order_seconds=wait_after_order_seconds,
                order_buying_power_buffer=order_buying_power_buffer,
            )
        )
    for idx, batch in enumerate(split_buy_plans_into_rounds(buy_plans, buy_submission_rounds), start=1):
        rows = submit_plans(
            client,
            batch,
            execute,
            f"{run_key}-b{idx}",
            wait_after_order_seconds=wait_after_order_seconds,
            order_buying_power_buffer=order_buying_power_buffer,
        )
        for row in rows:
            row["buy_round"] = idx
        submitted.extend(rows)
    return submitted


def execution_drift_report(
    orders: list[dict[str, Any]],
    target_qty: dict[str, float],
    final_qty: dict[str, float],
    open_order_count: int,
    evidence_cfg: dict[str, Any],
) -> dict[str, Any]:
    drift_cfg = evidence_cfg.get("execution_drift", {}) or {}
    if not bool(drift_cfg.get("enabled", False)):
        return {"enabled": False, "passed": True}

    symbols = sorted(set(target_qty) | set(final_qty))
    rows = []
    max_abs_drift = 0.0
    for symbol in symbols:
        target = float(target_qty.get(symbol, 0.0))
        final = float(final_qty.get(symbol, 0.0))
        drift = final - target
        max_abs_drift = max(max_abs_drift, abs(drift))
        rows.append(
            {
                "symbol": symbol,
                "target_qty": round(target, 6),
                "final_qty": round(final, 6),
                "drift_qty": round(drift, 6),
            }
        )

    planned_buy_notional = sum(float(order.get("notional_estimate") or 0.0) for order in orders if order.get("side") == "buy")
    filled_buy_notional = 0.0
    rejected_orders = []
    buy_orders = [order for order in orders if order.get("side") == "buy"]
    notional_buy_mode = bool(buy_orders) and all(as_float(order.get("qty")) == 0.0 for order in buy_orders)
    for order in orders:
        state = str(order.get("state", "")).lower()
        if state.startswith("rejected") or state.startswith("skipped"):
            rejected_orders.append(
                {
                    "symbol": order.get("symbol"),
                    "side": order.get("side"),
                    "state": order.get("state"),
                    "error": order.get("error"),
                }
            )
        if order.get("side") != "buy" or state not in FILLED_ORDER_STATES:
            continue
        filled_qty = as_float(order.get("filled_qty"))
        filled_price = as_float(order.get("filled_avg_price"))
        filled_buy_notional += filled_qty * filled_price

    max_symbol_qty_drift = int(drift_cfg.get("max_symbol_qty_drift", 1))
    max_open_orders = int(drift_cfg.get("max_open_orders_after_run", 0))
    min_notional_fill_ratio = float(drift_cfg.get("min_notional_fill_ratio", 0.98))
    fill_ratio = filled_buy_notional / planned_buy_notional if planned_buy_notional > 0.0 else None
    qty_drift_passed = max_abs_drift <= max_symbol_qty_drift
    notional_fill_passed = fill_ratio is not None and fill_ratio >= min_notional_fill_ratio
    passed = open_order_count <= max_open_orders and not rejected_orders and (
        qty_drift_passed or (notional_buy_mode and notional_fill_passed)
    )
    return {
        "enabled": True,
        "passed": passed,
        "mode": "notional_buy_fill" if notional_buy_mode else "quantity_drift",
        "max_symbol_qty_drift_allowed": max_symbol_qty_drift,
        "max_abs_symbol_qty_drift": round(max_abs_drift, 6),
        "qty_drift_passed": qty_drift_passed,
        "min_notional_fill_ratio": min_notional_fill_ratio,
        "notional_fill_passed": notional_fill_passed,
        "open_order_count_after_run": open_order_count,
        "planned_buy_notional": round(planned_buy_notional, 2),
        "filled_buy_notional": round(filled_buy_notional, 2),
        "buy_fill_ratio": round(fill_ratio, 6) if fill_ratio is not None else None,
        "rejected_or_skipped_orders": rejected_orders,
        "symbol_drifts": rows,
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--force-rebalance", action="store_true")
    parser.add_argument("--ignore-phase-lock", action="store_true")
    parser.add_argument("--ignore-daily-loss-block", action="store_true")
    parser.add_argument("--buy-only-rebalance", action="store_true")
    parser.add_argument("--buy-submission-rounds-override", type=int)
    parser.add_argument("--buying-power-buffer-override", type=float)
    parser.add_argument("--order-buying-power-buffer-override", type=float)
    args = parser.parse_args()

    config = load_yaml_config(args.config)
    strategy_cfg = config.get("pure_momentum", {})
    evidence_cfg = config.get("evidence", {})
    symbols = [str(symbol).upper() for symbol in strategy_cfg["universe"]]
    quantum_symbols = quantum_sleeve_symbols(strategy_cfg)
    defense_symbols = regime_defense_symbols(strategy_cfg)
    regime_symbol = str(strategy_cfg.get("regime_symbol", "SPY")).upper()
    data_symbols = sorted(set(symbols + quantum_symbols + defense_symbols + [regime_symbol]))
    universe = set(symbols + quantum_symbols + [symbol for symbol in defense_symbols if not symbol.startswith("^")])
    lookback_days = int(strategy_cfg.get("lookback_days", 63))
    rebalance_days = int(strategy_cfg.get("rebalance_days", 5))
    rebalance_phase_lock = bool(strategy_cfg.get("rebalance_phase_lock", False))
    rebalance_phase_anchor_date = strategy_cfg.get("rebalance_phase_anchor_date")
    rebalance_phase_anchor_date = str(rebalance_phase_anchor_date) if rebalance_phase_anchor_date else None
    sell_all_before_rebalance = bool(strategy_cfg.get("sell_all_before_rebalance", False))
    flatten_wait_seconds = int(strategy_cfg.get("flatten_wait_seconds", 60))
    order_fill_wait_seconds = int(strategy_cfg.get("order_fill_wait_seconds", 45))
    buy_submission_rounds = int(strategy_cfg.get("buy_submission_rounds", 10))
    if args.buy_submission_rounds_override:
        buy_submission_rounds = max(1, int(args.buy_submission_rounds_override))
    buying_power_buffer = (
        BUYING_POWER_BUFFER
        if args.buying_power_buffer_override is None
        else float(args.buying_power_buffer_override)
    )
    order_buying_power_buffer = (
        ORDER_BUYING_POWER_BUFFER
        if args.order_buying_power_buffer_override is None
        else float(args.order_buying_power_buffer_override)
    )
    target_gross = float(strategy_cfg.get("target_gross_leverage", 2.0))
    max_gross = float(strategy_cfg.get("max_gross_leverage", 2.0))
    strategy_name = str(strategy_cfg.get("name", "pure_momentum_mid_breadth_spy20_sleeve_gross18"))
    state = load_state(args.state_path)
    forward_lock = forward_lock_report(strategy_cfg, evidence_cfg)
    if not forward_lock["passed"]:
        report = {
            "status": "blocked_forward_lock",
            "message": "Forward-lock evidence is enabled and the configured strategy no longer matches the locked paper profile.",
            "strategy": strategy_name,
            "evidence": {"forward_lock": forward_lock},
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    client = AlpacaPaperTradingClient.from_env()
    account = client.get_account()
    if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked"):
        raise RuntimeError("Alpaca paper account is not active/tradable")

    clock = client.get_clock()
    close = fetch_close_frame(data_symbols, max(lookback_days, 252))
    anomaly_guard = anomaly_guard_report(close, data_symbols, evidence_cfg)
    if not anomaly_guard["passed"]:
        report = {
            "status": "blocked_data_anomaly",
            "message": "Data anomaly guard blocked paper orders before target calculation.",
            "strategy": strategy_name,
            "clock": clock,
            "evidence": {"forward_lock": forward_lock, "anomaly_guard": anomaly_guard},
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return
    selection = select_strategy_symbols(close, strategy_cfg, symbols)
    defense_guard = regime_defense_report(close, strategy_cfg)
    partial_guard = partial_defense_report(close, strategy_cfg, symbols)
    chosen = selection.symbols
    if not clock.get("is_open"):
        report = {
            "status": "market_closed",
            "message": "Market is closed; no paper orders submitted, but forward-lock signal evidence was recorded.",
            "strategy": strategy_name,
            "clock": clock,
            "selected_symbols": chosen,
            "selection_mode": selection.mode,
            "breadth20": selection.breadth20,
            "spy20": selection.spy20,
            "spy_dd63": selection.spy_dd63,
            "regime_defense_guard": defense_guard,
            "partial_defense_guard": partial_guard,
            "evidence": {"forward_lock": forward_lock, "anomaly_guard": anomaly_guard},
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    positions = client.get_positions()
    current_qty = managed_positions(positions, universe)
    due, due_reason, trading_days_elapsed = is_rebalance_due(close, state, rebalance_days, bool(current_qty))
    defense_symbol = str(defense_guard.get("defense_symbol", "")).upper()
    defense_position_active = bool(defense_symbol and current_qty.get(defense_symbol))
    partial_defense_symbol = str(partial_guard.get("defense_symbol", "")).upper()
    partial_position_active = bool(partial_defense_symbol and current_qty.get(partial_defense_symbol))
    if defense_guard.get("active") and current_qty != {defense_symbol: current_qty.get(defense_symbol, 0)}:
        due = True
        due_reason = "regime_defense_entry"
    elif defense_position_active and not defense_guard.get("active"):
        due = True
        due_reason = "regime_defense_exit"
    elif partial_guard.get("active") and not partial_position_active:
        due = True
        due_reason = "partial_defense_entry"
    elif partial_position_active and not partial_guard.get("active"):
        due = True
        due_reason = "partial_defense_exit"
    phase_days_elapsed = (
        trading_days_since(close, rebalance_phase_anchor_date)
        if rebalance_phase_anchor_date
        else trading_days_elapsed
    )
    phase_status = rebalance_phase_status(
        state,
        phase_days_elapsed,
        rebalance_days,
        rebalance_phase_lock,
        rebalance_phase_anchor_date,
    )
    phase_sleeves_cfg = strategy_cfg.get("phase_sleeves") or []
    phase_sleeves_enabled = bool(phase_sleeves_cfg)
    phase_sleeve_offsets = {int(item.get("phase_offset", 0)) for item in phase_sleeves_cfg}
    phase_sleeve_missing_state = any(
        not state.get("phase_sleeves", {}).get(str(item.get("name", f"phase_{item.get('phase_offset', 0)}")), {}).get("last_rebalance_date")
        for item in phase_sleeves_cfg
    )
    completed_signal_date = close.index[-1].date().isoformat()
    if phase_sleeves_enabled and phase_status.get("phase_offset") in phase_sleeve_offsets:
        due = True
        due_reason = "phase_sleeve_due"
    elif (
        not args.force_rebalance
        and rebalance_phase_anchor_date
        and phase_status["enabled"]
        and phase_status["aligned"]
        and state.get("last_rebalance_date") != completed_signal_date
    ):
        due = True
        due_reason = "phase_anchor_due"
    due = args.force_rebalance or due
    if args.force_rebalance:
        due_reason = "forced"
    open_orders = client.get_orders(status="open", nested=True)

    if has_blocking_open_order(open_orders, universe):
        report = {
            "status": "blocked_open_order",
            "message": "Open managed-symbol order exists; duplicate paper orders blocked.",
            "selected_symbols": chosen,
            "selection_mode": selection.mode,
            "last_rebalance_date": state.get("last_rebalance_date"),
            "trading_days_since_rebalance": trading_days_elapsed,
            "rebalance_phase": phase_status,
            "rebalance_due_reason": due_reason,
            "regime_defense_guard": defense_guard,
            "partial_defense_guard": partial_guard,
            "evidence": {"forward_lock": forward_lock, "anomaly_guard": anomaly_guard},
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    phase_allowed = (
        phase_status["aligned"]
        or (phase_sleeves_enabled and phase_status.get("phase_offset") in phase_sleeve_offsets)
        or phase_sleeve_missing_state
    )
    if due and phase_status["enabled"] and not phase_allowed and not args.ignore_phase_lock:
        report = {
            "status": "blocked_phase_lock",
            "message": "Rebalance phase lock is enabled; no paper orders submitted outside the locked 7-trading-day phase.",
            "selected_symbols": chosen,
            "selection_mode": selection.mode,
            "current_qty": current_qty,
            "last_rebalance_date": state.get("last_rebalance_date"),
            "trading_days_since_rebalance": trading_days_elapsed,
            "rebalance_phase": phase_status,
            "phase_sleeves_enabled": phase_sleeves_enabled,
            "phase_sleeve_offsets": sorted(phase_sleeve_offsets),
            "rebalance_due_reason": due_reason,
            "force_rebalance": bool(args.force_rebalance),
            "regime_defense_guard": defense_guard,
            "partial_defense_guard": partial_guard,
            "evidence": {"forward_lock": forward_lock, "anomaly_guard": anomaly_guard},
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    if not due:
        report = {
            "status": "not_rebalance_day",
            "message": "Pure Momentum 63 rebalance is not due today.",
            "selected_symbols": chosen,
            "selection_mode": selection.mode,
            "current_qty": current_qty,
            "last_rebalance_date": state.get("last_rebalance_date"),
            "trading_days_since_rebalance": trading_days_elapsed,
            "rebalance_phase": phase_status,
            "rebalance_due_reason": due_reason,
            "regime_defense_guard": defense_guard,
            "partial_defense_guard": partial_guard,
            "evidence": {"forward_lock": forward_lock, "anomaly_guard": anomaly_guard},
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    equity = float(account.get("equity") or 0.0)
    buying_power = float(account.get("buying_power") or 0.0)
    flatten_orders: list[dict[str, Any]] = []
    if sell_all_before_rebalance and current_qty and not args.buy_only_rebalance:
        flatten_plans = build_liquidation_plan(close, current_qty)
        run_key = datetime.now(timezone.utc).strftime("%Y%m%d")
        flatten_orders = submit_plans(
            client,
            flatten_plans,
            args.execute,
            f"{run_key}-flat",
            wait_after_order_seconds=order_fill_wait_seconds,
            order_buying_power_buffer=order_buying_power_buffer,
        )
        remaining_positions, remaining_open_orders = wait_for_flatten(client, universe, flatten_wait_seconds) if args.execute else ({}, [])
        if remaining_positions or has_blocking_open_order(remaining_open_orders, universe):
            report = {
                "status": "flatten_submitted_waiting_rebalance",
                "execute": bool(args.execute),
                "strategy": strategy_name,
                "selected_symbols": chosen,
                "current_qty": current_qty,
                "remaining_qty": remaining_positions,
                "rebalance_due": due,
                "rebalance_due_reason": due_reason,
                "last_rebalance_date": state.get("last_rebalance_date"),
                "trading_days_since_rebalance": trading_days_elapsed,
                "rebalance_phase": phase_status,
                "sell_all_before_rebalance": sell_all_before_rebalance,
                "flatten_orders": flatten_orders,
                "message": "Flatten orders were submitted; buy rebalance will wait until managed positions and open orders clear.",
                "evidence": {"forward_lock": forward_lock, "anomaly_guard": anomaly_guard},
                "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            }
            write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
            print(json.dumps(report, indent=2))
            return
        account = client.get_account()
        equity = float(account.get("equity") or 0.0)
        buying_power = float(account.get("buying_power") or 0.0)
        current_qty = {}

    active_target_gross = selection.target_gross_override if selection.target_gross_override is not None else target_gross
    phase_sleeve_reports: list[dict[str, Any]] = []
    phase_sleeves_state_updated = False
    quantum_sleeve_report: dict[str, Any] = {"enabled": False}
    target_exposures: dict[str, float] = {}
    target_qty: dict[str, float] = {}
    if defense_guard.get("active"):
        defense_symbol = str(defense_guard["defense_symbol"]).upper()
        target_exposures = {defense_symbol: float(defense_guard.get("defense_exposure", 1.0))}
        chosen = [defense_symbol]
        active_target_gross = sum(target_exposures.values())
        target_qty = target_qty_from_exposures(close, target_exposures, equity, max_gross)
        plans = build_plan_from_target_exposures(close, target_exposures, current_qty, equity, max_gross)
    elif phase_sleeves_enabled:
        target_exposures, chosen, phase_sleeve_reports, phase_sleeves_state_updated = combined_phase_sleeve_targets(
            close,
            strategy_cfg,
            symbols,
            state,
            rebalance_phase_anchor_date,
            rebalance_days,
            target_gross,
            phase_status.get("phase_offset"),
            bool(args.force_rebalance),
        )
        quantum_exposures, quantum_sleeve_report = quantum_sleeve_targets(close, strategy_cfg)
        quantum_exposures, quantum_sleeve_report = apply_quantum_sleeve_defense(
            quantum_exposures,
            quantum_sleeve_report,
            partial_guard,
            strategy_cfg,
        )
        for symbol, exposure in quantum_exposures.items():
            target_exposures[symbol] = target_exposures.get(symbol, 0.0) + exposure
        target_exposures = apply_partial_defense_overlay(target_exposures, partial_guard)
        chosen = sorted(set(chosen) | set(quantum_exposures))
        if partial_guard.get("active"):
            chosen = sorted(set(chosen) | {str(partial_guard["defense_symbol"]).upper()})
        active_target_gross = sum(target_exposures.values())
        target_qty = target_qty_from_exposures(close, target_exposures, equity, max_gross)
        plans = build_plan_from_target_exposures(close, target_exposures, current_qty, equity, max_gross)
    else:
        target_exposures = target_exposures_from_selection(chosen, active_target_gross, selection.target_weights)
        quantum_exposures, quantum_sleeve_report = quantum_sleeve_targets(close, strategy_cfg)
        quantum_exposures, quantum_sleeve_report = apply_quantum_sleeve_defense(
            quantum_exposures,
            quantum_sleeve_report,
            partial_guard,
            strategy_cfg,
        )
        for symbol, exposure in quantum_exposures.items():
            target_exposures[symbol] = target_exposures.get(symbol, 0.0) + exposure
        target_exposures = apply_partial_defense_overlay(target_exposures, partial_guard)
        chosen = sorted(set(chosen) | set(quantum_exposures))
        if partial_guard.get("active"):
            chosen = sorted(set(chosen) | {str(partial_guard["defense_symbol"]).upper()})
        active_target_gross = sum(target_exposures.values())
        target_qty = target_qty_from_exposures(close, target_exposures, equity, max_gross)
        plans = build_plan_from_target_exposures(close, target_exposures, current_qty, equity, max_gross)
    if args.buy_only_rebalance:
        plans = [plan for plan in plans if plan.side == "buy"]
    daily_loss_blocked_buys = should_block_new_buys(account)
    if daily_loss_blocked_buys and not args.ignore_daily_loss_block:
        plans = [plan for plan in plans if plan.side == "sell"]

    plans, buying_power_adjustment = fit_buys_to_buying_power(
        plans,
        buying_power,
        buying_power_buffer=buying_power_buffer,
    )
    buy_notional = sum(plan.notional for plan in plans if plan.side == "buy")

    run_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    submitted = submit_rebalance_plans(
        client,
        plans,
        args.execute,
        run_key,
        wait_after_order_seconds=order_fill_wait_seconds,
        buy_submission_rounds=buy_submission_rounds,
        order_buying_power_buffer=order_buying_power_buffer,
    )
    final_account = client.get_account() if args.execute else account
    final_positions = managed_positions(client.get_positions(), universe) if args.execute else current_qty
    final_open_orders = client.get_orders(status="open", nested=True) if args.execute else []
    execution_drift = execution_drift_report(
        submitted,
        target_qty,
        final_positions,
        len(final_open_orders),
        evidence_cfg,
    )
    completed_signal_date = str(close.index[-1].date())
    state_updated = False
    if args.execute and not args.buy_only_rebalance:
        state.update(
            {
                "last_rebalance_date": completed_signal_date,
                "last_rebalance_at_utc": datetime.now(timezone.utc).isoformat(),
                "last_selected_symbols": chosen,
                "last_order_count": len(submitted),
                "last_rebalance_reason": due_reason,
                "strategy": strategy_name,
            }
        )
        save_state(args.state_path, state)
        state_updated = True
    elif args.execute and phase_sleeves_state_updated:
        save_state(args.state_path, state)
        state_updated = True
    report = {
        "status": "submitted" if args.execute else "dry_run",
        "execute": bool(args.execute),
        "strategy": strategy_name,
        "selected_symbols": chosen,
        "selection_mode": selection.mode,
        "breadth20": selection.breadth20,
        "spy20": selection.spy20,
        "spy_dd63": selection.spy_dd63,
        "current_qty": current_qty,
        "target_gross_leverage": target_gross,
        "active_target_gross_leverage": active_target_gross,
        "selection_target_gross_override": selection.target_gross_override,
        "selection_target_weights": selection.target_weights,
        "max_gross_leverage": max_gross,
        "target_exposures": target_exposures,
        "target_qty": target_qty,
        "phase_sleeves_enabled": phase_sleeves_enabled,
        "phase_sleeves": phase_sleeve_reports,
        "quantum_sleeve": quantum_sleeve_report,
        "regime_defense_guard": defense_guard,
        "partial_defense_guard": partial_guard,
        "rebalance_due": due,
        "rebalance_due_reason": due_reason,
        "last_rebalance_date": state.get("last_rebalance_date"),
        "trading_days_since_rebalance": trading_days_elapsed,
        "rebalance_phase": phase_status,
        "daily_loss_block": {
            "triggered": daily_loss_blocked_buys,
            "ignored": bool(args.ignore_daily_loss_block),
            "threshold_pct": DAILY_BUY_BLOCK_LOSS_PCT * 100.0,
        },
        "state_updated": state_updated,
        "sell_all_before_rebalance": sell_all_before_rebalance,
        "buy_submission_rounds": buy_submission_rounds,
        "buying_power_buffer": buying_power_buffer,
        "order_buying_power_buffer": order_buying_power_buffer,
        "buy_only_rebalance": bool(args.buy_only_rebalance),
        "flatten_orders": flatten_orders,
        "buying_power": buying_power,
        "buying_power_adjustment": buying_power_adjustment,
        "planned_buy_notional": round(buy_notional, 2),
        "equity": equity,
        "final_buying_power": float(final_account.get("buying_power") or 0.0),
        "final_equity": float(final_account.get("equity") or 0.0),
        "final_qty": final_positions,
        "open_order_count_after_run": len(final_open_orders),
        "orders": submitted,
        "evidence": {
            "forward_lock": forward_lock,
            "anomaly_guard": anomaly_guard,
            "execution_drift": execution_drift,
        },
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
