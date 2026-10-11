"""Phase-aware marks from captured prices; no execution or external I/O."""
from dataclasses import replace
from types import MappingProxyType

from backtesting.primary_state import (
    EventTime, Mark, MarkQuality, Phase, PortfolioState, PreparedPrimaryData, number,
)


class PriceBook:
    """Separate open/close lookup tables: opening callers never receive a full bar."""

    def __init__(self, data: PreparedPrimaryData):
        if not data.sessions or tuple(sorted(set(data.sessions))) != data.sessions:
            raise ValueError("Invalid canonical sessions")
        if not data.symbols or tuple(sorted(set(data.symbols))) != data.symbols:
            raise ValueError("Invalid canonical symbols")
        if sorted(f.symbol for f in data.frames) != list(data.symbols):
            raise ValueError("Frame universe does not match symbols")
        self.sessions = data.sessions
        self.symbols = data.symbols
        tables = {Phase.OPEN: {}, Phase.CLOSE: {}}
        for frame in data.frames:
            if len(frame.rows) != len(frame.dates) or tuple(sorted(set(frame.dates))) != frame.dates:
                raise ValueError("Malformed prepared frame")
            if not set(frame.dates) <= set(data.sessions):
                raise ValueError("Frame outside calendar")
            for phase, column in ((Phase.OPEN, 'open'), (Phase.CLOSE, 'close')):
                index = frame.columns.index(column)
                for day, row in zip(frame.dates, frame.rows):
                    price = row[index]
                    number(price, f'{column} price', positive=True)
                    tables[phase][(frame.symbol, day)] = price
        self._tables = MappingProxyType({phase: MappingProxyType(table) for phase, table in tables.items()})

    def price(self, symbol: str, at: EventTime) -> float | None:
        if symbol not in self.symbols or at.session not in self.sessions:
            raise ValueError("Unknown symbol/session")
        return self._tables[at.phase].get((symbol, at.session))


def mark_at(book: PriceBook, symbol: str, at: EventTime, previous: Mark | None = None) -> Mark:
    if previous is not None and previous.as_of > at:
        raise ValueError("Cannot carry a future mark backwards")
    price = book.price(symbol, at)
    if price is not None:
        return Mark(price, at, at, MarkQuality.CURRENT)
    if previous is None or previous.value is None:
        return Mark(None, None, at, MarkQuality.MISSING)
    return Mark(previous.value, previous.observed_at, at, MarkQuality.STALE)


def mark_portfolio(state: PortfolioState, book: PriceBook, at: EventTime) -> PortfolioState:
    """Return a fully revalued copy; failure leaves caller state untouched."""
    state.validate()
    if at < state.at:
        raise ValueError("Portfolio marking cannot move backwards")
    positions = {}
    for symbol, position in sorted(state.positions.items()):
        mark = mark_at(book, symbol, at, position.mark)
        if mark.value is None:
            raise ValueError("A held position has no defensible historical mark")
        positions[symbol] = replace(position, mark=mark)
    result = replace(state, at=at, positions=positions, pending=dict(state.pending))
    result.equity = result.cash + result.exposure
    result.validate()
    return result


def has_stale_holdings(state: PortfolioState) -> bool:
    return any(p.mark.quality != MarkQuality.CURRENT for p in state.positions.values())
