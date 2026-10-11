"""Price availability, stale carrying, and phase-specific marking tests."""
from dataclasses import replace
from datetime import date

import pandas as pd
import pytest

from backtesting.primary_inputs import prepare_primary_data
from backtesting.primary_marking import PriceBook, mark_at, mark_portfolio
from backtesting.primary_state import EventTime, Phase, Mark, MarkQuality, Position, PortfolioState


def prices():
    dates = pd.to_datetime(['2024-01-05', '2024-01-09', '2024-01-10'])
    frame = pd.DataFrame(dict(open=[100., 120.], high=[111., 131.], low=[99., 119.],
                              close=[110., 130.], volume=[1., 1.]), index=dates[[0, 2]])
    return prepare_primary_data({'A': frame}, [], session_index=dates,
        metadata=dict(calendar_source='archived_exchange_calendar', price_source='synthetic', adjustment_policy='none'))


def test_only_current_phase_price_is_available():
    book = PriceBook(prices())
    opening = EventTime(date(2024, 1, 5), Phase.OPEN)
    closing = replace(opening, phase=Phase.CLOSE)
    assert book.price('A', opening) == 100.
    assert book.price('A', closing) == 110.
    assert mark_at(book, 'A', opening).observed_at == opening
    with pytest.raises(ValueError, match='future'):
        mark_at(book, 'A', opening, mark_at(book, 'A', closing))


def test_missing_is_not_a_zero_price_and_stale_retains_source():
    book = PriceBook(prices())
    prior = EventTime(date(2024, 1, 5), Phase.CLOSE)
    opening = EventTime(date(2024, 1, 9), Phase.OPEN)
    missing = mark_at(book, 'A', opening)
    assert missing.quality == MarkQuality.MISSING and missing.value is None
    stale = mark_at(book, 'A', opening, mark_at(book, 'A', prior))
    assert stale.value == 110. and stale.observed_at == prior and stale.as_of == opening
    assert stale.quality == MarkQuality.STALE
    closing = mark_at(book, 'A', replace(opening, phase=Phase.CLOSE), stale)
    assert closing.observed_at == prior and closing.quality == MarkQuality.STALE
    resumed = mark_at(book, 'A', EventTime(date(2024, 1, 10), Phase.OPEN), closing)
    assert resumed.value == 120. and resumed.quality == MarkQuality.CURRENT


def test_marking_reconciles_without_changing_cash_or_cost_basis():
    book = PriceBook(prices())
    entry = EventTime(date(2024, 1, 5), Phase.OPEN)
    position = Position('A', 5., 'entry', EventTime(date(2024, 1, 4), Phase.CLOSE),
                        entry, 100., 100., 0., 500., 95., 120., mark_at(book, 'A', entry))
    state = PortfolioState(entry, 1000., 500., 1000., positions={'A': position})
    closing = mark_portfolio(state, book, replace(entry, phase=Phase.CLOSE))
    assert closing.cash == state.cash == 500.
    assert closing.equity == 1050. and closing.exposure == 550.
    assert closing.positions['A'].cost_basis == 500.
    assert state.positions['A'].mark.value == 100.
    stale = mark_portfolio(closing, book, EventTime(date(2024, 1, 9), Phase.OPEN))
    assert stale.equity == 1050.
    assert stale.positions['A'].mark.quality == MarkQuality.STALE
    with pytest.raises(ValueError, match='backwards'):
        mark_portfolio(stale, book, entry)


@pytest.mark.parametrize('bad', [0., -1., float('nan'), float('inf')])
def test_invalid_execution_prices_fail_before_any_ledger_can_run(bad):
    data = prices()
    frame = data.frames[0]
    row = list(frame.rows[0])
    row[frame.columns.index('open')] = bad
    malformed = replace(data, frames=(replace(frame, rows=(tuple(row),)+frame.rows[1:]),))
    with pytest.raises(ValueError):
        PriceBook(malformed)


def test_no_fabricated_holiday_bar():
    book = PriceBook(prices())
    with pytest.raises(ValueError, match='session'):
        book.price('A', EventTime(date(2024, 1, 8), Phase.OPEN))
