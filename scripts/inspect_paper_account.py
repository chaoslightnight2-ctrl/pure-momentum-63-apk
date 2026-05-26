from __future__ import annotations

import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.trading.alpaca_paper import ALPACA_PAPER_TRADING_URL, AlpacaPaperTradingClient
from src.utils.io_utils import ensure_dir, write_json


def compact_position(position: dict[str, Any]) -> dict[str, Any]:
    return {
        "symbol": position.get("symbol"),
        "qty": position.get("qty"),
        "market_value": position.get("market_value"),
        "cost_basis": position.get("cost_basis"),
        "unrealized_pl": position.get("unrealized_pl"),
        "unrealized_plpc": position.get("unrealized_plpc"),
    }


def compact_order(order: dict[str, Any]) -> dict[str, Any]:
    return {
        "id": order.get("id"),
        "symbol": order.get("symbol"),
        "side": order.get("side"),
        "type": order.get("type"),
        "qty": order.get("qty"),
        "status": order.get("status"),
        "submitted_at": order.get("submitted_at"),
    }


def main() -> None:
    client = AlpacaPaperTradingClient.from_env()
    if client.base_url != ALPACA_PAPER_TRADING_URL:
        raise RuntimeError(f"Refusing non-paper Alpaca endpoint: {client.base_url}")

    account = client.get_account()
    positions = [compact_position(position) for position in client.get_positions()]
    open_orders = [compact_order(order) for order in client.get_orders(status="open", nested=True)]
    report = {
        "paper_only": True,
        "account": {
            "status": account.get("status"),
            "trading_blocked": account.get("trading_blocked"),
            "equity": account.get("equity"),
            "last_equity": account.get("last_equity"),
            "cash": account.get("cash"),
            "buying_power": account.get("buying_power"),
            "portfolio_value": account.get("portfolio_value"),
            "daytrade_count": account.get("daytrade_count"),
        },
        "positions": positions,
        "open_orders": open_orders,
        "position_count": len(positions),
        "open_order_count": len(open_orders),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ensure_dir(ROOT / "reports") / "inspect_paper_account.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
