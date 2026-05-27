from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from scripts.run_pure_momentum_paper import fetch_close_frame, load_yaml_config, select_strategy_symbols
from src.trading.alpaca_paper import ALPACA_PAPER_TRADING_URL, AlpacaPaperTradingClient
from src.utils.io_utils import ensure_dir, write_json


def as_float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def compact_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": position.get("symbol"),
        "qty": as_float(position.get("qty")),
        "market_value": as_float(position.get("market_value")),
        "cost_basis": as_float(position.get("cost_basis")),
        "unrealized_pl": as_float(position.get("unrealized_pl")),
        "unrealized_plpc": as_float(position.get("unrealized_plpc")),
        "avg_entry_price": as_float(position.get("avg_entry_price")),
        "current_price": as_float(position.get("current_price")),
    }


def compact_fill(activity: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": activity.get("id"),
        "order_id": activity.get("order_id"),
        "transaction_time": activity.get("transaction_time"),
        "symbol": activity.get("symbol"),
        "side": activity.get("side"),
        "qty": as_float(activity.get("qty")),
        "price": as_float(activity.get("price")),
        "net_amount": as_float(activity.get("net_amount")),
    }


def fills_by_symbol(fills: list[dict[str, Any]]) -> list[dict[str, Any]]:
    grouped: dict[str, dict[str, float]] = defaultdict(lambda: defaultdict(float))
    for fill in fills:
        symbol = str(fill.get("symbol") or "").upper()
        if not symbol:
            continue
        side = str(fill.get("side") or "").lower()
        qty = as_float(fill.get("qty"))
        price = as_float(fill.get("price"))
        notional = qty * price
        row = grouped[symbol]
        row["fill_count"] += 1
        if side == "buy":
            row["buy_qty"] += qty
            row["buy_notional"] += notional
        elif side == "sell":
            row["sell_qty"] += qty
            row["sell_notional"] += notional
    out = []
    for symbol, row in grouped.items():
        buy_qty = row["buy_qty"]
        sell_qty = row["sell_qty"]
        out.append(
            {
                "symbol": symbol,
                "fill_count": int(row["fill_count"]),
                "buy_qty": buy_qty,
                "buy_notional": row["buy_notional"],
                "avg_buy_price": row["buy_notional"] / buy_qty if buy_qty else None,
                "sell_qty": sell_qty,
                "sell_notional": row["sell_notional"],
                "avg_sell_price": row["sell_notional"] / sell_qty if sell_qty else None,
                "cashflow_pnl_proxy": row["sell_notional"] - row["buy_notional"],
            }
        )
    return sorted(out, key=lambda r: abs(float(r["cashflow_pnl_proxy"])), reverse=True)


def parse_portfolio_history(history: dict[str, Any]) -> dict[str, Any]:
    equity = [as_float(x) for x in history.get("equity") or []]
    profit_loss = [as_float(x) for x in history.get("profit_loss") or []]
    profit_loss_pct = [as_float(x) for x in history.get("profit_loss_pct") or []]
    timestamps = history.get("timestamp") or []
    if not equity:
        return {
            "points": 0,
            "start_equity": None,
            "end_equity": None,
            "total_pl": None,
            "total_return_pct": None,
            "max_drawdown_pct": None,
        }
    series = pd.Series(equity)
    max_dd = float((series / series.cummax() - 1.0).min() * 100.0)
    start_equity = equity[0]
    end_equity = equity[-1]
    return {
        "points": len(equity),
        "first_timestamp": timestamps[0] if timestamps else None,
        "last_timestamp": timestamps[-1] if timestamps else None,
        "start_equity": start_equity,
        "end_equity": end_equity,
        "total_pl": end_equity - start_equity,
        "total_return_pct": (end_equity / start_equity - 1.0) * 100.0 if start_equity else None,
        "max_drawdown_pct": max_dd,
        "last_profit_loss": profit_loss[-1] if profit_loss else None,
        "last_profit_loss_pct": profit_loss_pct[-1] if profit_loss_pct else None,
    }


def current_strategy_signal(config_path: Path) -> dict[str, Any]:
    config = load_yaml_config(config_path)
    strategy_cfg = config.get("pure_momentum", {})
    symbols = [str(symbol).upper() for symbol in strategy_cfg.get("universe", [])]
    regime_symbol = str(strategy_cfg.get("regime_symbol", "SPY")).upper()
    lookback_days = int(strategy_cfg.get("lookback_days", 63))
    close = fetch_close_frame(symbols + [regime_symbol], max(lookback_days, 80))
    selection = select_strategy_symbols(close, strategy_cfg, symbols)
    return {
        "strategy": strategy_cfg.get("name"),
        "completed_signal_date": str(close.index[-1].date()),
        "mode": selection.mode,
        "symbols": selection.symbols,
        "breadth20": selection.breadth20,
        "spy20": selection.spy20,
        "spy_dd63": selection.spy_dd63,
        "target_gross_override": selection.target_gross_override,
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only Alpaca paper performance report.")
    parser.add_argument("--after", default="2026-05-11", help="Start date for fills and portfolio history.")
    parser.add_argument("--until", default=None, help="Optional end date.")
    parser.add_argument("--config", default="config/settings.yaml")
    args = parser.parse_args()

    client = AlpacaPaperTradingClient.from_env()
    if client.base_url != ALPACA_PAPER_TRADING_URL:
        raise RuntimeError(f"Refusing non-paper Alpaca endpoint: {client.base_url}")

    account = client.get_account()
    positions = [compact_position(position) for position in client.get_positions()]
    open_orders = client.get_orders(status="open", nested=True)

    activity_params = {
        "activity_types": "FILL",
        "after": args.after,
        "direction": "asc",
        "page_size": 100,
    }
    if args.until:
        activity_params["until"] = args.until
    fills = [compact_fill(activity) for activity in client._request("GET", "/v2/account/activities", params=activity_params)]

    history_params = {"timeframe": "1D", "date_start": args.after}
    if args.until:
        history_params["date_end"] = args.until
    history = client._request("GET", "/v2/account/portfolio/history", params=history_params)
    parsed_history = parse_portfolio_history(history)

    total_unrealized = sum(position["unrealized_pl"] for position in positions)
    total_market_value = sum(abs(position["market_value"]) for position in positions)
    equity = as_float(account.get("equity"))

    report = {
        "paper_only": True,
        "read_only": True,
        "period": {"after": args.after, "until": args.until},
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        "account": {
            "status": account.get("status"),
            "trading_blocked": account.get("trading_blocked"),
            "equity": equity,
            "last_equity": as_float(account.get("last_equity")),
            "cash": as_float(account.get("cash")),
            "buying_power": as_float(account.get("buying_power")),
            "portfolio_value": as_float(account.get("portfolio_value")),
        },
        "portfolio_history": parsed_history,
        "current_exposure": {
            "position_count": len(positions),
            "gross_market_value": total_market_value,
            "gross_exposure": total_market_value / equity if equity else None,
            "total_unrealized_pl": total_unrealized,
        },
        "current_positions": sorted(positions, key=lambda p: abs(p["market_value"]), reverse=True),
        "fills": {
            "count": len(fills),
            "by_symbol": fills_by_symbol(fills),
            "raw": fills,
        },
        "open_order_count": len(open_orders),
        "current_strategy_signal": current_strategy_signal(ROOT / args.config),
    }

    out_path = ensure_dir(ROOT / "reports") / "paper_performance_since_2026-05-11.json"
    write_json(out_path, report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
