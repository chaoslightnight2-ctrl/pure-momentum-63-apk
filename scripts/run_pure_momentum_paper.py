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
BUYING_POWER_BUFFER = 0.98
ORDER_BUYING_POWER_BUFFER = 0.85
MANAGED_ORDER_PREFIX = "gh-puremom"
DEFAULT_STATE_PATH = "state/pure_momentum_state.json"
OPEN_ORDER_STATES = {"new", "accepted", "pending_new", "partially_filled", "pending_replace", "pending_cancel"}


@dataclass
class PaperOrderPlan:
    symbol: str
    side: str
    qty: int
    price: float
    target_qty: int
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
            payload["qty"] = str(self.qty)
        return payload

    def resized(self, qty: int) -> "PaperOrderPlan":
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
        return SelectionResult([], "insufficient_regime_data", None, None, None)

    breadth20 = float(((close[universe].iloc[-1] / close[universe].iloc[-breadth_lookback - 1] - 1.0) > 0.0).mean())
    spy = close[spy_symbol]
    spy20 = float(spy.iloc[-1] / spy.iloc[-breadth_lookback - 1] - 1.0)
    spy_dd63 = float(spy.iloc[-1] / spy.iloc[-63:].max() - 1.0)

    if spy20 < -0.05:
        return SelectionResult(
            rank_symbols(close, universe, 5, 7, min_momentum),
            "loss_spy20_crash_lb5_top7",
            breadth20,
            spy20,
            spy_dd63,
        )
    if -0.05 <= spy_dd63 <= -0.02:
        if spydd_score == "acceleration_20_minus_63":
            return SelectionResult(
                rank_symbols_spydd_acceleration(close, universe, spydd_top_n, min_momentum, spydd_min_mom20, spydd_min_mom5),
                "loss_spy_dd_2_5_accel20_minus63_top2",
                breadth20,
                spy20,
                spy_dd63,
            )
        return SelectionResult(
            rank_symbols(close, universe, lookback_days, 5, min_momentum),
            "loss_spy_dd_2_5_lb63_top5",
            breadth20,
            spy20,
            spy_dd63,
        )
    if breadth20 < weak_breadth:
        if weak_breadth_score == "composite_63_20_5":
            return SelectionResult(
                rank_symbols_composite_momentum(close, universe, 5, min_momentum, weak_breadth_score_weights),
                "loss_weak_breadth_comp_40_40_20_top5",
                breadth20,
                spy20,
                spy_dd63,
            )
        return SelectionResult(
            rank_symbols(close, universe, lookback_days, 5, min_momentum),
            "loss_weak_breadth_lb63_top5",
            breadth20,
            spy20,
            spy_dd63,
        )
    if -0.02 <= spy20 < 0.0:
        return SelectionResult(
            rank_symbols(close, universe, lookback_days, 5, min_momentum),
            "loss_spy20_mild_neg_lb63_top5",
            breadth20,
            spy20,
            spy_dd63,
        )
    if mid_low <= breadth20 < mid_high and mid_sleeve_gross is not None and spy20 >= mid_sleeve_spy20_min:
        return SelectionResult(
            rank_symbols(close, universe, lookback_days, mid_sleeve_top_n, min_momentum),
            "normal_mid_breadth_spy20_top3_gross13",
            breadth20,
            spy20,
            spy_dd63,
            mid_sleeve_gross,
        )
    if mid_low <= breadth20 < mid_high:
        return SelectionResult([], "normal_mid_breadth_cash", breadth20, spy20, spy_dd63)
    return SelectionResult(
        rank_symbols(close, universe, lookback_days, top_n, min_momentum, min_short_momentum, normal_above_ma_days),
        "normal_lb63_top7_mom5_pos_above20ma" if normal_above_ma_days == 20 else "normal_lb63_top7_mom5_pos",
        breadth20,
        spy20,
        spy_dd63,
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


def managed_positions(positions: list[dict[str, Any]], universe: set[str]) -> dict[str, int]:
    out: dict[str, int] = {}
    for position in positions:
        symbol = str(position.get("symbol", "")).upper()
        if symbol not in universe:
            continue
        qty = float(position.get("qty", 0.0))
        out[symbol] = int(math.floor(abs(qty))) if qty > 0 else -int(math.floor(abs(qty)))
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
    current_qty: dict[str, int],
    equity: float,
    target_gross: float,
    max_gross: float,
) -> list[PaperOrderPlan]:
    target_gross = min(target_gross, max_gross)
    target_weight = target_gross / len(selected) if selected else 0.0
    target_qty: dict[str, int] = {}
    for symbol in selected:
        price = float(close[symbol].iloc[-1])
        target_qty[symbol] = int(math.floor((equity * target_weight) / price)) if price > 0 else 0

    plans: list[PaperOrderPlan] = []
    all_symbols = sorted(set(current_qty) | set(target_qty))
    for symbol in all_symbols:
        price = float(close[symbol].iloc[-1]) if symbol in close.columns else 0.0
        if price <= 0.0:
            continue
        desired = target_qty.get(symbol, 0)
        current = current_qty.get(symbol, 0)
        delta = desired - current
        if abs(delta) < 1:
            continue
        side = "buy" if delta > 0 else "sell"
        qty = abs(int(delta))
        if side == "sell":
            qty = min(qty, max(current, 0))
        if qty < 1 or qty * price < MIN_DELTA_NOTIONAL:
            continue
        plans.append(PaperOrderPlan(symbol=symbol, side=side, qty=qty, price=price, target_qty=desired))
    return sorted(plans, key=lambda plan: 0 if plan.side == "sell" else 1)


def build_liquidation_plan(close: pd.DataFrame, current_qty: dict[str, int]) -> list[PaperOrderPlan]:
    plans: list[PaperOrderPlan] = []
    for symbol in sorted(current_qty):
        qty = current_qty[symbol]
        price = float(close[symbol].iloc[-1]) if symbol in close.columns else 0.0
        if price <= 0.0 or qty == 0:
            continue
        side = "sell" if qty > 0 else "buy"
        order_qty = abs(int(qty))
        if order_qty < 1 or order_qty * price < MIN_DELTA_NOTIONAL:
            continue
        plans.append(PaperOrderPlan(symbol=symbol, side=side, qty=order_qty, price=price, target_qty=0))
    return plans


def wait_for_flatten(
    client: AlpacaPaperTradingClient,
    universe: set[str],
    timeout_seconds: int,
) -> tuple[dict[str, int], list[dict[str, Any]]]:
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
) -> tuple[list[PaperOrderPlan], dict[str, Any]]:
    buys = [plan for plan in plans if plan.side == "buy"]
    buy_notional = sum(plan.notional for plan in buys)
    available = max(0.0, buying_power * BUYING_POWER_BUFFER)
    info = {
        "applied": False,
        "original_buy_notional": round(buy_notional, 2),
        "available_buy_notional": round(available, 2),
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
    batches: list[list[PaperOrderPlan]] = []
    allocated = {plan.symbol: 0 for plan in buys}
    for round_idx in range(1, rounds + 1):
        batch: list[PaperOrderPlan] = []
        for plan in buys:
            cumulative = int(math.floor(plan.qty * round_idx / rounds))
            qty = cumulative - allocated[plan.symbol]
            if qty < 1 or qty * plan.price < MIN_DELTA_NOTIONAL:
                continue
            allocated[plan.symbol] += qty
            batch.append(plan.resized(qty))
        if batch:
            batches.append(batch)
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
    effective_buying_power = min(buying_power, daytrading_buying_power)
    remaining_buy_count = max(1, remaining_buy_count)
    notional_cap = (effective_buying_power * ORDER_BUYING_POWER_BUFFER) / remaining_buy_count
    affordable_qty = int(math.floor(notional_cap / plan.price)) if plan.price > 0 else 0
    capped_qty = min(plan.qty, affordable_qty)
    info = {
        "applied": capped_qty < plan.qty,
        "buying_power": round(buying_power, 2),
        "daytrading_buying_power": round(daytrading_buying_power, 2),
        "effective_buying_power": round(effective_buying_power, 2),
        "original_qty": plan.qty,
        "capped_qty": capped_qty,
    }
    if capped_qty < 1 or capped_qty * plan.price < MIN_DELTA_NOTIONAL:
        fractional_notional = min(plan.notional, notional_cap)
        if fractional_notional >= MIN_DELTA_NOTIONAL:
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
) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    for plan_idx, plan in enumerate(plans):
        cap_info: dict[str, Any] = {"applied": False}
        if execute:
            remaining_buy_count = sum(1 for item in plans[plan_idx:] if item.side == "buy")
            capped_plan, cap_info = cap_buy_to_current_buying_power(client, plan, remaining_buy_count)
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
                retry_notional = buying_power * ORDER_BUYING_POWER_BUFFER if buying_power else 0.0
                retry_qty = int(math.floor(retry_notional / plan.price)) if buying_power else 0
                retry_qty = min(plan.qty - 1, retry_qty)
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
                elif plan.side == "buy" and retry_notional >= MIN_DELTA_NOTIONAL:
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
            )
        )
    for idx, batch in enumerate(split_buy_plans_into_rounds(buy_plans, buy_submission_rounds), start=1):
        rows = submit_plans(
            client,
            batch,
            execute,
            f"{run_key}-b{idx}",
            wait_after_order_seconds=wait_after_order_seconds,
        )
        for row in rows:
            row["buy_round"] = idx
        submitted.extend(rows)
    return submitted


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
    args = parser.parse_args()

    config = load_yaml_config(args.config)
    strategy_cfg = config.get("pure_momentum", {})
    symbols = [str(symbol).upper() for symbol in strategy_cfg["universe"]]
    regime_symbol = str(strategy_cfg.get("regime_symbol", "SPY")).upper()
    data_symbols = sorted(set(symbols + [regime_symbol]))
    universe = set(symbols)
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
    target_gross = float(strategy_cfg.get("target_gross_leverage", 2.0))
    max_gross = float(strategy_cfg.get("max_gross_leverage", 2.0))
    strategy_name = str(strategy_cfg.get("name", "pure_momentum_mid_breadth_spy20_sleeve_gross18"))
    state = load_state(args.state_path)

    client = AlpacaPaperTradingClient.from_env()
    account = client.get_account()
    if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked"):
        raise RuntimeError("Alpaca paper account is not active/tradable")

    clock = client.get_clock()
    if not clock.get("is_open"):
        report = {"status": "market_closed", "message": "Market is closed; no paper orders submitted.", "clock": clock}
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    close = fetch_close_frame(data_symbols, max(lookback_days, 63))
    selection = select_strategy_symbols(close, strategy_cfg, symbols)
    chosen = selection.symbols
    positions = client.get_positions()
    current_qty = managed_positions(positions, universe)
    due, due_reason, trading_days_elapsed = is_rebalance_due(close, state, rebalance_days, bool(current_qty))
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
    completed_signal_date = close.index[-1].date().isoformat()
    if (
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
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    if due and phase_status["enabled"] and not phase_status["aligned"] and not args.ignore_phase_lock:
        report = {
            "status": "blocked_phase_lock",
            "message": "Rebalance phase lock is enabled; no paper orders submitted outside the locked 7-trading-day phase.",
            "selected_symbols": chosen,
            "selection_mode": selection.mode,
            "current_qty": current_qty,
            "last_rebalance_date": state.get("last_rebalance_date"),
            "trading_days_since_rebalance": trading_days_elapsed,
            "rebalance_phase": phase_status,
            "rebalance_due_reason": due_reason,
            "force_rebalance": bool(args.force_rebalance),
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
    plans = build_plan(close, chosen, current_qty, equity, active_target_gross, max_gross)
    if args.buy_only_rebalance:
        plans = [plan for plan in plans if plan.side == "buy"]
    daily_loss_blocked_buys = should_block_new_buys(account)
    if daily_loss_blocked_buys and not args.ignore_daily_loss_block:
        plans = [plan for plan in plans if plan.side == "sell"]

    plans, buying_power_adjustment = fit_buys_to_buying_power(plans, buying_power)
    buy_notional = sum(plan.notional for plan in plans if plan.side == "buy")

    run_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    submitted = submit_rebalance_plans(
        client,
        plans,
        args.execute,
        run_key,
        wait_after_order_seconds=order_fill_wait_seconds,
        buy_submission_rounds=buy_submission_rounds,
    )
    final_account = client.get_account() if args.execute else account
    final_positions = managed_positions(client.get_positions(), universe) if args.execute else current_qty
    final_open_orders = client.get_orders(status="open", nested=True) if args.execute else []
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
        "max_gross_leverage": max_gross,
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
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
