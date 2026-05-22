from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import pandas as pd
import requests

from src.utils.io_utils import ensure_dir
from src.utils.time_utils import to_utc_index


ALPACA_DATA_URL = "https://data.alpaca.markets"


def load_dotenv_if_present(path: str | Path = ".env") -> None:
    env_path = Path(path)
    if not env_path.exists():
        return
    for raw_line in env_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


class AlpacaDataClient:
    """Read-only Alpaca market data client.

    This client never touches trading endpoints. Missing keys or unavailable
    feeds return empty frames so the pipeline can fail closed through the data
    quality gate instead of pretending data is good.
    """

    def __init__(self) -> None:
        load_dotenv_if_present()
        self.api_key = os.getenv("ALPACA_API_KEY", "")
        self.secret_key = os.getenv("ALPACA_SECRET_KEY", "")

    @property
    def available(self) -> bool:
        return bool(self.api_key and self.secret_key)

    def _headers(self) -> dict[str, str]:
        return {
            "APCA-API-KEY-ID": self.api_key,
            "APCA-API-SECRET-KEY": self.secret_key,
        }

    def fetch_bars(
        self,
        symbol: str,
        start: str,
        end: str,
        timeframe: str = "15Min",
        feed: str | None = None,
        adjustment: str = "all",
        chunk_days: int | None = None,
    ) -> pd.DataFrame:
        if not self.available:
            return pd.DataFrame()
        feed = feed or os.getenv("ALPACA_FEED", "iex")
        chunk_days = int(chunk_days or os.getenv("ALPACA_CHUNK_DAYS", "180"))
        start_ts = pd.Timestamp(start, tz="UTC")
        end_ts = pd.Timestamp(end, tz="UTC")
        if chunk_days > 0 and (end_ts - start_ts).days > chunk_days:
            frames = []
            chunk_start = start_ts
            while chunk_start < end_ts:
                chunk_end = min(chunk_start + pd.Timedelta(days=chunk_days), end_ts)
                frame = self._fetch_bars_cached(symbol, chunk_start, chunk_end, timeframe, feed, adjustment)
                if not frame.empty:
                    frames.append(frame)
                chunk_start = chunk_end
            if not frames:
                return pd.DataFrame()
            return to_utc_index(pd.concat(frames).sort_index()).loc[lambda frame: ~frame.index.duplicated(keep="last")]

        return self._fetch_bars_cached(symbol, start_ts, end_ts, timeframe, feed, adjustment)

    def _cache_path(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        timeframe: str,
        feed: str,
        adjustment: str,
    ) -> Path:
        safe = f"{symbol.upper()}_{timeframe}_{feed}_{adjustment}_{start:%Y%m%d}_{end:%Y%m%d}.csv"
        return ensure_dir(Path("data") / "cache" / "alpaca" / "bars") / safe

    def _fetch_bars_cached(
        self,
        symbol: str,
        start: pd.Timestamp,
        end: pd.Timestamp,
        timeframe: str,
        feed: str,
        adjustment: str,
    ) -> pd.DataFrame:
        cache_path = self._cache_path(symbol, start, end, timeframe, feed, adjustment)
        if cache_path.exists():
            frame = pd.read_csv(cache_path, parse_dates=["timestamp"]).set_index("timestamp")
            return to_utc_index(frame)

        url = f"{ALPACA_DATA_URL}/v2/stocks/{symbol}/bars"
        params: dict[str, Any] = {
            "start": start.isoformat(),
            "end": end.isoformat(),
            "timeframe": timeframe,
            "adjustment": adjustment,
            "feed": feed,
            "limit": 10000,
        }
        rows: list[dict[str, Any]] = []
        while True:
            response = requests.get(url, headers=self._headers(), params=params, timeout=30)
            if response.status_code >= 400:
                return pd.DataFrame()
            payload = response.json()
            rows.extend(payload.get("bars", []))
            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = token

        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows).rename(
            columns={
                "t": "timestamp",
                "o": "open",
                "h": "high",
                "l": "low",
                "c": "close",
                "v": "volume",
                "n": "trade_count",
                "vw": "vwap",
            }
        )
        frame["timestamp"] = pd.to_datetime(frame["timestamp"], utc=True)
        frame = frame.set_index("timestamp").sort_index()
        frame["provider"] = "alpaca"
        frame = to_utc_index(frame)
        frame.to_csv(cache_path, index=True, index_label="timestamp")
        return frame

    def fetch_news(self, symbol: str, start: str, end: str) -> pd.DataFrame:
        if not self.available:
            return pd.DataFrame()
        url = f"{ALPACA_DATA_URL}/v1beta1/news"
        params: dict[str, Any] = {
            "symbols": symbol,
            "start": pd.Timestamp(start, tz="UTC").isoformat(),
            "end": pd.Timestamp(end, tz="UTC").isoformat(),
            "limit": 50,
        }
        rows: list[dict[str, Any]] = []
        while True:
            response = requests.get(url, headers=self._headers(), params=params, timeout=30)
            if response.status_code >= 400:
                return pd.DataFrame()
            payload = response.json()
            rows.extend(payload.get("news", []))
            token = payload.get("next_page_token")
            if not token:
                break
            params["page_token"] = token
        if not rows:
            return pd.DataFrame()
        frame = pd.DataFrame(rows)
        frame["timestamp"] = pd.to_datetime(frame.get("created_at"), utc=True)
        frame["headline"] = frame.get("headline", "").fillna("")
        frame["summary"] = frame.get("summary", "").fillna("")
        return frame[["timestamp", "headline", "summary"]].sort_values("timestamp")


def fetch_alpaca_bars(symbol: str, start: str, end: str, timeframe: str = "15Min") -> pd.DataFrame:
    return AlpacaDataClient().fetch_bars(symbol, start, end, timeframe=timeframe)
