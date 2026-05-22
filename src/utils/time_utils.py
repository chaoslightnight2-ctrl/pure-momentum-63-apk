from __future__ import annotations

from datetime import timedelta

import pandas as pd
from pandas.tseries.holiday import GoodFriday, USFederalHolidayCalendar

US_EASTERN = "America/New_York"


def to_utc_index(frame: pd.DataFrame) -> pd.DataFrame:
    out = frame.copy()
    out.index = pd.to_datetime(out.index)
    if out.index.tz is None:
        out.index = out.index.tz_localize("UTC")
    else:
        out.index = out.index.tz_convert("UTC")
    out = out.sort_index()
    return out


def is_regular_market_timestamp(index: pd.DatetimeIndex) -> pd.Series:
    eastern = index.tz_convert(US_EASTERN)
    minutes = eastern.hour * 60 + eastern.minute
    weekday = eastern.weekday < 5
    in_session = (minutes >= 9 * 60 + 30) & (minutes < 16 * 60)
    on_grid = (eastern.minute % 15) == 0
    return pd.Series(weekday & in_session & on_grid, index=index)


def expected_market_15m_index(start, end) -> pd.DatetimeIndex:
    start = pd.Timestamp(start)
    end = pd.Timestamp(end)
    if start.tzinfo is None:
        start = start.tz_localize("UTC")
    else:
        start = start.tz_convert("UTC")
    if end.tzinfo is None:
        end = end.tz_localize("UTC")
    else:
        end = end.tz_convert("UTC")
    idx = pd.date_range(start.floor("D"), end.ceil("D"), freq="15min", tz="UTC")
    mask = is_regular_market_timestamp(idx)
    expected = idx[mask.values]
    if expected.empty:
        return expected
    local_dates = expected.tz_convert(US_EASTERN).normalize().tz_localize(None)
    federal_holidays = USFederalHolidayCalendar().holidays(local_dates.min(), local_dates.max())
    good_friday = GoodFriday.dates(local_dates.min(), local_dates.max())
    closed_dates = federal_holidays.union(good_friday)
    return expected[~local_dates.isin(closed_dates)]


def minutes_since_last_event(bar_index: pd.DatetimeIndex, events: pd.DataFrame) -> pd.Series:
    if events is None or events.empty:
        return pd.Series(float("inf"), index=bar_index)
    event_times = pd.to_datetime(events["timestamp"], utc=True).sort_values()
    result = []
    last_pos = -1
    for ts in bar_index:
        while last_pos + 1 < len(event_times) and event_times.iloc[last_pos + 1] <= ts:
            last_pos += 1
        if last_pos < 0:
            result.append(float("inf"))
        else:
            result.append((ts - event_times.iloc[last_pos]).total_seconds() / 60.0)
    return pd.Series(result, index=bar_index)


def next_regular_market_open(ts: pd.Timestamp) -> pd.Timestamp:
    ts = pd.Timestamp(ts)
    if ts.tzinfo is None:
        ts = ts.tz_localize("UTC")
    local = ts.tz_convert(US_EASTERN)
    open_time = local.replace(hour=9, minute=30, second=0, microsecond=0)
    close_time = local.replace(hour=16, minute=0, second=0, microsecond=0)
    if local.weekday() < 5 and local <= open_time:
        return open_time.tz_convert("UTC")
    if local.weekday() < 5 and local < close_time:
        return local.ceil("15min").tz_convert("UTC")
    nxt = (local + timedelta(days=1)).replace(hour=9, minute=30, second=0, microsecond=0)
    while nxt.weekday() >= 5:
        nxt += timedelta(days=1)
    return nxt.tz_convert("UTC")
