"""
Trading-session counting for the time-based exit — exchange sessions
(what Alpaca's calendar says the market was actually open for), never
calendar days. A position entered Thursday and evaluated the following
Wednesday has been held 4 trading sessions (Fri, Mon, Tue, Wed), not 6
calendar days.

Two counting paths, both counting sessions the same way, for two
different data sources:

- Live/paper trading: Alpaca's own market calendar (get_calendar), the
  same source functions/market_hours_trading.is_market_hours() already
  trusts for "is the market open right now" — reused here for "which
  days was it open."
- Backtesting: the historical OHLCV DataFrame's own DatetimeIndex. Each
  row in that index IS a session the market was open and this ticker
  traded — no separate calendar call needed or more correct, since it's
  the exact same session set the rest of the backtest walks over.

Exit execution timing convention (documented once, here, rather than
repeated at every call site): a time-based exit that becomes due is
evaluated using that session's data and executed at that session's close
in the backtest (consistent with how evaluate_exit's stop/target checks
already work against a single daily bar) or as a market order at the
next available paper-trading opportunity live — never backdated.
"""

from datetime import date, datetime

import pandas as pd


def sessions_held_backtest(index: pd.DatetimeIndex, entry_date, as_of_date) -> int:
    """Number of trading sessions strictly after entry_date, up to and
    including as_of_date, per the DataFrame's own index. entry_date itself
    is session 0 held (the entry day), so a position entered and evaluated
    on the very next available bar has been held for 1 session."""
    entry_ts = pd.Timestamp(entry_date)
    as_of_ts = pd.Timestamp(as_of_date)
    mask = (index > entry_ts) & (index <= as_of_ts)
    return int(mask.sum())


def sessions_held_live(alpaca_client, entry_date: date, as_of_date: date | None = None) -> int:
    """Same convention as sessions_held_backtest, but against Alpaca's
    live market calendar. Returns None-safe: raises rather than silently
    returning 0 if the calendar call fails, so a caller can't mistake
    "couldn't check" for "just entered.\""""
    as_of_date = as_of_date or datetime.utcnow().date()
    from alpaca.trading.requests import GetCalendarRequest

    sessions = alpaca_client.get_calendar(GetCalendarRequest(start=entry_date, end=as_of_date))
    session_dates = {s.date if isinstance(s.date, date) else s.date.date() for s in sessions}
    session_dates.discard(entry_date)
    return len(session_dates)
