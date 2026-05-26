from __future__ import annotations

import argparse
import json
import math
import os
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd
import yfinance as yf

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.trading.alpaca_paper import AlpacaPaperTradingClient
from src.utils.io_utils import ensure_dir, load_yaml_config, write_json


DAILY_BUY_BLOCK_LOSS_PCT = 0.02
MIN_DELTA_NOTIONAL = 25.0
BUYING_POWER_BUFFER = 0.98
MANAGED_ORDER_PREFIX = "gh-puremom"
DEFAULT_STATE_PATH = "state/pure_momentum_state.json"


@dataclass
class PaperOrderPlan:
    symbol: str
    side: str
    qty: int
    price: float
    target_qty: int

    @property
    def notional(self) -> float:
        return float(self.qty * self.price)

    def payload(self, run_key: str) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "qty": str(self.qty),
            "side": self.side,
            "type": "market",
            "time_in_force": "day",
            "client_order_id": f"{MANAGED_ORDER_PREFIX}-{run_key}-{self.symbol.lower()}-{self.side}",
        }


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


def selected_symbols(close: pd.DataFrame, lookback_days: int, top_n: int, min_momentum: float | None) -> list[str]:
    momentum = close.iloc[-1] / close.iloc[-lookback_days - 1] - 1.0
    momentum = momentum.replace([float("inf"), float("-inf")], pd.NA).dropna().sort_values(ascending=False)
    if min_momentum is not None:
        momentum = momentum.loc[momentum >= min_momentum]
    return [str(symbol) for symbol in momentum.head(top_n).index]


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


def submit_plans(
    client: AlpacaPaperTradingClient,
    plans: list[PaperOrderPlan],
    execute: bool,
    run_key: str,
) -> list[dict[str, Any]]:
    submitted: list[dict[str, Any]] = []
    for plan in plans:
        payload = plan.payload(run_key)
        row = {
            "symbol": plan.symbol,
            "side": plan.side,
            "qty": plan.qty,
            "notional_estimate": round(plan.notional, 2),
            "client_order_id": payload["client_order_id"],
            "state": "planned",
        }
        if execute:
            result = client.submit_order(payload)
            row["state"] = result.get("status", "submitted")
            row["alpaca_order_id"] = result.get("id")
            time.sleep(0.5)
        submitted.append(row)
    return submitted


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="config/settings.yaml")
    parser.add_argument("--state-path", default=DEFAULT_STATE_PATH)
    parser.add_argument("--execute", action="store_true")
    parser.add_argument("--force-rebalance", action="store_true")
    args = parser.parse_args()

    config = load_yaml_config(args.config)
    strategy_cfg = config.get("pure_momentum", {})
    symbols = [str(symbol).upper() for symbol in strategy_cfg["universe"]]
    universe = set(symbols)
    lookback_days = int(strategy_cfg.get("lookback_days", 63))
    top_n = int(strategy_cfg.get("top_n", 3))
    rebalance_days = int(strategy_cfg.get("rebalance_days", 5))
    target_gross = float(strategy_cfg.get("target_gross_leverage", 2.0))
    max_gross = float(strategy_cfg.get("max_gross_leverage", 2.0))
    min_momentum_raw = strategy_cfg.get("min_momentum")
    min_momentum = None if min_momentum_raw is None else float(min_momentum_raw)
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

    close = fetch_close_frame(symbols, lookback_days)
    chosen = selected_symbols(close, lookback_days, top_n, min_momentum)
    positions = client.get_positions()
    current_qty = managed_positions(positions, universe)
    due, due_reason, trading_days_elapsed = is_rebalance_due(close, state, rebalance_days, bool(current_qty))
    due = args.force_rebalance or due
    if args.force_rebalance:
        due_reason = "forced"
    open_orders = client.get_orders(status="open", nested=True)

    if has_blocking_open_order(open_orders, universe):
        report = {
            "status": "blocked_open_order",
            "message": "Open managed-symbol order exists; duplicate paper orders blocked.",
            "selected_symbols": chosen,
            "last_rebalance_date": state.get("last_rebalance_date"),
            "trading_days_since_rebalance": trading_days_elapsed,
            "rebalance_due_reason": due_reason,
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    if not due:
        report = {
            "status": "not_rebalance_day",
            "message": "Pure Momentum 63 rebalance is not due today.",
            "selected_symbols": chosen,
            "current_qty": current_qty,
            "last_rebalance_date": state.get("last_rebalance_date"),
            "trading_days_since_rebalance": trading_days_elapsed,
            "rebalance_due_reason": due_reason,
        }
        write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
        print(json.dumps(report, indent=2))
        return

    equity = float(account.get("equity") or 0.0)
    buying_power = float(account.get("buying_power") or 0.0)
    plans = build_plan(close, chosen, current_qty, equity, target_gross, max_gross)
    if should_block_new_buys(account):
        plans = [plan for plan in plans if plan.side == "sell"]

    plans, buying_power_adjustment = fit_buys_to_buying_power(plans, buying_power)
    buy_notional = sum(plan.notional for plan in plans if plan.side == "buy")

    run_key = datetime.now(timezone.utc).strftime("%Y%m%d")
    submitted = submit_plans(client, plans, args.execute, run_key)
    completed_signal_date = str(close.index[-1].date())
    state_updated = False
    if args.execute:
        state.update(
            {
                "last_rebalance_date": completed_signal_date,
                "last_rebalance_at_utc": datetime.now(timezone.utc).isoformat(),
                "last_selected_symbols": chosen,
                "last_order_count": len(submitted),
                "last_rebalance_reason": due_reason,
                "strategy": "pure_momentum_lb63_top3_reb5",
            }
        )
        save_state(args.state_path, state)
        state_updated = True
    report = {
        "status": "submitted" if args.execute else "dry_run",
        "execute": bool(args.execute),
        "strategy": "pure_momentum_lb63_top3_reb5",
        "selected_symbols": chosen,
        "current_qty": current_qty,
        "target_gross_leverage": target_gross,
        "max_gross_leverage": max_gross,
        "rebalance_due": due,
        "rebalance_due_reason": due_reason,
        "last_rebalance_date": state.get("last_rebalance_date"),
        "trading_days_since_rebalance": trading_days_elapsed,
        "state_updated": state_updated,
        "buying_power": buying_power,
        "buying_power_adjustment": buying_power_adjustment,
        "planned_buy_notional": round(buy_notional, 2),
        "equity": equity,
        "orders": submitted,
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ensure_dir(ROOT / "reports") / "pure_momentum_paper_run.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
