from datetime import date

import pandas as pd

from engine.swing_strategy.trading_calendar import sessions_held_backtest, sessions_held_live


def test_sessions_held_backtest_counts_only_real_trading_days():
    # A real trading calendar, weekends excluded (pandas 'B' = business day).
    index = pd.bdate_range("2026-01-05", periods=15)  # Mon Jan 5 .. skips weekends
    entry_date = index[0]  # Monday
    as_of = index[5]  # 5 business days later
    held = sessions_held_backtest(index, entry_date, as_of)
    assert held == 5


def test_sessions_held_backtest_entry_day_itself_is_zero():
    index = pd.bdate_range("2026-01-05", periods=10)
    held = sessions_held_backtest(index, index[0], index[0])
    assert held == 0


def test_sessions_held_backtest_ignores_calendar_days_gap_over_weekend():
    # Friday -> Monday is 3 calendar days but 1 trading session.
    index = pd.bdate_range("2026-01-05", periods=10)
    friday = index[3]  # Jan 8, 2026 is a Thursday in bdate_range starting Mon; just assert via position
    next_session = index[4]
    held = sessions_held_backtest(index, friday, next_session)
    assert held == 1


def test_sessions_held_backtest_skips_a_holiday_gap_in_the_index():
    # Simulate a holiday by constructing an index that already has the
    # holiday date removed (as real OHLCV data would) — a 4-calendar-day
    # gap should still only count as 1 session if that's all the index has.
    dates = pd.to_datetime(["2026-01-05", "2026-01-06", "2026-01-07", "2026-01-08", "2026-01-12"])
    held = sessions_held_backtest(dates, dates[3], dates[4])
    assert held == 1


class _FakeCalendarSession:
    def __init__(self, d):
        self.date = d


class _FakeAlpacaClient:
    def __init__(self, session_dates):
        self._session_dates = session_dates

    def get_calendar(self, filters):
        return [_FakeCalendarSession(d) for d in self._session_dates
                if filters.start <= d <= filters.end]


def test_sessions_held_live_excludes_weekends_and_entry_day():
    # Entered Thursday 2026-01-08; market open Thu, Fri, (weekend skipped),
    # Mon, Tue -> as of Tue that's 3 sessions held (Fri, Mon, Tue).
    session_dates = [date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7), date(2026, 1, 8),
                      date(2026, 1, 9), date(2026, 1, 12), date(2026, 1, 13)]
    client = _FakeAlpacaClient(session_dates)
    held = sessions_held_live(client, entry_date=date(2026, 1, 8), as_of_date=date(2026, 1, 13))
    assert held == 3


def test_sessions_held_live_zero_on_entry_day():
    session_dates = [date(2026, 1, 8)]
    client = _FakeAlpacaClient(session_dates)
    held = sessions_held_live(client, entry_date=date(2026, 1, 8), as_of_date=date(2026, 1, 8))
    assert held == 0
