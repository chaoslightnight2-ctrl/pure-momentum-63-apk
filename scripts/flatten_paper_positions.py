from __future__ import annotations

import argparse
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.trading.alpaca_paper import ALPACA_PAPER_TRADING_URL, AlpacaPaperTradingClient
from src.utils.io_utils import ensure_dir, write_json


CONFIRM_TEXT = "FLATTEN_PAPER_POSITIONS"


def cancel_open_orders(client: AlpacaPaperTradingClient) -> list[dict[str, Any]]:
    cancelled: list[dict[str, Any]] = []
    for order in client.get_orders(status="open", nested=True):
        order_id = str(order.get("id") or "")
        if not order_id:
            continue
        client.cancel_order(order_id)
        cancelled.append(
            {
                "id": order_id,
                "symbol": order.get("symbol"),
                "side": order.get("side"),
                "type": order.get("type"),
                "status": "cancel_requested",
            }
        )
        time.sleep(0.5)
    return cancelled


def close_positions(client: AlpacaPaperTradingClient) -> list[dict[str, Any]]:
    closed: list[dict[str, Any]] = []
    positions = client.get_positions()
    for position in positions:
        symbol = str(position.get("symbol") or "").upper()
        if not symbol:
            continue
        result = client.close_position(symbol)
        closed.append(
            {
                "symbol": symbol,
                "qty": position.get("qty"),
                "market_value": position.get("market_value"),
                "close_order_id": (result or {}).get("id") if isinstance(result, dict) else None,
                "status": (result or {}).get("status", "submitted") if isinstance(result, dict) else "submitted",
            }
        )
        time.sleep(0.5)
    return closed


def main() -> None:
    parser = argparse.ArgumentParser(description="Close all Alpaca paper positions.")
    parser.add_argument("--confirm", required=True)
    args = parser.parse_args()

    if args.confirm != CONFIRM_TEXT:
        raise RuntimeError(f"Refusing to flatten without --confirm {CONFIRM_TEXT}")

    client = AlpacaPaperTradingClient.from_env()
    if client.base_url != ALPACA_PAPER_TRADING_URL:
        raise RuntimeError(f"Refusing non-paper Alpaca endpoint: {client.base_url}")

    account = client.get_account()
    if str(account.get("status", "")).upper() != "ACTIVE" or account.get("trading_blocked"):
        raise RuntimeError("Alpaca paper account is not active/tradable")

    clock = client.get_clock()
    if not clock.get("is_open"):
        report = {
            "status": "market_closed",
            "message": "Market is closed; no flatten orders submitted.",
            "clock": clock,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
        }
        write_json(ensure_dir(ROOT / "reports") / "flatten_paper_positions.json", report)
        print(json.dumps(report, indent=2))
        return

    cancelled = cancel_open_orders(client)
    closed = close_positions(client)
    report = {
        "status": "submitted",
        "paper_only": True,
        "cancelled_open_orders": cancelled,
        "closed_positions": closed,
        "closed_position_count": len(closed),
        "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    }
    write_json(ensure_dir(ROOT / "reports") / "flatten_paper_positions.json", report)
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
