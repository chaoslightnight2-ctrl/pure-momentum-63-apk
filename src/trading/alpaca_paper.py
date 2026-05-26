from __future__ import annotations

import os
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import requests

from src.data.alpaca_data import load_dotenv_if_present


ALPACA_PAPER_TRADING_URL = "https://paper-api.alpaca.markets"


@dataclass
class AlpacaPaperTradingClient:
    api_key: str
    secret_key: str
    base_url: str = ALPACA_PAPER_TRADING_URL
    min_request_interval_seconds: float = 0.35
    max_retries: int = 3
    _last_request_at: float = 0.0

    @classmethod
    def from_env(cls, env_path: str | Path = ".env") -> "AlpacaPaperTradingClient":
        load_dotenv_if_present(env_path)
        return cls(
            api_key=os.getenv("ALPACA_API_KEY", ""),
            secret_key=os.getenv("ALPACA_SECRET_KEY", ""),
        )

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _headers(self) -> dict[str, str]:
        if not self.available:
            raise RuntimeError("Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in environment/.env")
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
            "Content-Type": "application/json",
        }

    def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        elapsed = time.monotonic() - self._last_request_at
        if elapsed < self.min_request_interval_seconds:
            time.sleep(self.min_request_interval_seconds - elapsed)

        url = f"{self.base_url}{path}"
        last_response: requests.Response | None = None
        for attempt in range(self.max_retries + 1):
            response = requests.request(method, url, headers=self._headers(), timeout=30, **kwargs)
            self._last_request_at = time.monotonic()
            if response.status_code not in {429, 500, 502, 503, 504}:
                self._raise_for_status_with_body(response)
                if response.status_code == 204 or not response.content:
                    return None
                return response.json()
            last_response = response
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                delay = float(retry_after)
            else:
                delay = min(8.0, 1.0 * (2**attempt))
            time.sleep(delay)
        if last_response is not None:
            self._raise_for_status_with_body(last_response)
        raise RuntimeError(f"Alpaca request failed: {method} {path}")

    def _raise_for_status_with_body(self, response: requests.Response) -> None:
        try:
            response.raise_for_status()
        except requests.HTTPError as exc:
            body = response.text.strip()
            if body:
                raise requests.HTTPError(f"{exc}; response body: {body}", response=response) from exc
            raise

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def get_clock(self) -> dict[str, Any]:
        return self._request("GET", "/v2/clock")

    def get_asset(self, symbol: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/assets/{symbol.upper()}")

    def get_positions(self) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/positions")

    def get_orders(self, status: str = "open", nested: bool = True) -> list[dict[str, Any]]:
        return self._request("GET", "/v2/orders", params={"status": status, "nested": str(nested).lower()})

    def get_order(self, order_id: str) -> dict[str, Any]:
        return self._request("GET", f"/v2/orders/{order_id}")

    def cancel_order(self, order_id: str) -> None:
        try:
            self._request("DELETE", f"/v2/orders/{order_id}")
        except requests.HTTPError as exc:
            if exc.response is None or exc.response.status_code != 404:
                raise

    def submit_order(self, order: dict[str, Any]) -> dict[str, Any]:
        return self._request("POST", "/v2/orders", json=order)

    def close_position(self, symbol: str) -> dict[str, Any]:
        return self._request("DELETE", f"/v2/positions/{symbol}")
