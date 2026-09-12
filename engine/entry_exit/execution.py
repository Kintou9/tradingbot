"""
Shared trade-execution helper: places the broker order, waits briefly for
a fill, and writes Trade/Position rows from the ACTUAL fill price rather
than a pre-trade estimate.

Why this exists as its own module rather than inline in each caller: the
2026-09-05 MSFT entry was recorded at its pre-fill quote ($499.70) while
Alpaca actually filled at $492.80, and the mismatch silently threw off
displayed P&L until it was caught and hand-corrected days later. Used by
the dashboard's manual buy/sell endpoints (api/main.py) — the automated
loop in functions/market_hours_trading.py still records its own
technical-scan entry price and is unchanged by this module.
"""

import time

from alpaca.trading.enums import OrderStatus

from broker.alpaca_client import client as alpaca_client, place_market_order
from db.models import Position, Trade
from notifications.sms import notify_trade


def _wait_for_fill(order, timeout_s: float = 8.0, poll_s: float = 0.5):
    """Paper/live market orders here have filled near-instantly in
    practice, but poll rather than assume — a slow fill (or a rejection)
    should surface as an error, not get recorded as a fictitious trade."""
    deadline = time.time() + timeout_s
    latest = order
    while time.time() < deadline:
        latest = alpaca_client.get_order_by_id(order.id)
        if latest.status == OrderStatus.FILLED:
            return latest
        time.sleep(poll_s)
    return latest


def execute_buy(db, ticker: str, qty: float, reason: str,
                 stop_loss_price: float | None = None,
                 take_profit_price: float | None = None) -> dict:
    order = place_market_order(ticker, qty=qty, side="buy")
    filled = _wait_for_fill(order)
    fill_price = float(filled.filled_avg_price) if filled.filled_avg_price else None
    filled_qty = float(filled.filled_qty) if filled.filled_qty else qty
    if fill_price is None:
        raise RuntimeError(
            f"{ticker} buy order {order.id} hasn't filled yet (status={filled.status}) — "
            "check Alpaca before retrying; nothing was recorded to the DB."
        )

    db.add(Trade(ticker=ticker, action="buy", quantity=filled_qty, price=fill_price, reason=reason))
    existing = db.query(Position).filter(Position.ticker == ticker).first()
    if existing:
        # Averaging into an existing position — blend the entry price
        # rather than overwrite it.
        total_qty = existing.quantity + filled_qty
        existing.avg_entry_price = (
            existing.avg_entry_price * existing.quantity + fill_price * filled_qty
        ) / total_qty
        existing.quantity = total_qty
        if stop_loss_price is not None:
            existing.stop_loss_price = stop_loss_price
        if take_profit_price is not None:
            existing.take_profit_price = take_profit_price
    else:
        db.add(Position(
            ticker=ticker,
            quantity=filled_qty,
            avg_entry_price=fill_price,
            stop_loss_price=stop_loss_price,
            take_profit_price=take_profit_price,
        ))
    db.commit()
    notify_trade(ticker, "buy", filled_qty, fill_price, reason)
    return {"ticker": ticker, "action": "buy", "qty": filled_qty, "price": fill_price, "order_id": str(order.id)}


def execute_sell(db, ticker: str, qty: float, reason: str) -> dict:
    order = place_market_order(ticker, qty=qty, side="sell")
    filled = _wait_for_fill(order)
    fill_price = float(filled.filled_avg_price) if filled.filled_avg_price else None
    filled_qty = float(filled.filled_qty) if filled.filled_qty else qty
    if fill_price is None:
        raise RuntimeError(
            f"{ticker} sell order {order.id} hasn't filled yet (status={filled.status}) — "
            "check Alpaca before retrying; nothing was recorded to the DB."
        )

    db.add(Trade(ticker=ticker, action="sell", quantity=filled_qty, price=fill_price, reason=reason))
    position_row = db.query(Position).filter(Position.ticker == ticker).first()
    if position_row:
        remaining = position_row.quantity - filled_qty
        if remaining <= 0.0001:
            db.delete(position_row)
        else:
            position_row.quantity = remaining
    db.commit()
    notify_trade(ticker, "sell", filled_qty, fill_price, reason)
    return {"ticker": ticker, "action": "sell", "qty": filled_qty, "price": fill_price, "order_id": str(order.id)}
