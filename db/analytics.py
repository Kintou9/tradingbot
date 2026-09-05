"""Aggregate stats derived from trade history. Trade rows log each fill
(buy/sell, qty, price) but not realized P&L directly, so this matches
sells against prior buys FIFO per ticker to work it out.
"""

from collections import defaultdict, deque

from .models import Trade


def compute_trade_summary(trades: list[Trade]) -> dict:
    """trades must be ordered oldest-first for FIFO matching to be correct."""
    open_lots = defaultdict(deque)  # ticker -> deque of [qty, price] buy lots
    realized_pl = 0.0
    closed_trades = 0
    winning_trades = 0

    for t in trades:
        lots = open_lots[t.ticker]
        if t.action == "buy":
            lots.append([t.quantity, t.price])
        elif t.action == "sell":
            remaining = t.quantity
            trade_pl = 0.0
            while remaining > 0 and lots:
                lot_qty, lot_price = lots[0]
                matched_qty = min(remaining, lot_qty)
                trade_pl += matched_qty * (t.price - lot_price)
                remaining -= matched_qty
                lot_qty -= matched_qty
                if lot_qty <= 0:
                    lots.popleft()
                else:
                    lots[0][0] = lot_qty
            realized_pl += trade_pl
            closed_trades += 1
            if trade_pl > 0:
                winning_trades += 1

    return {
        "realized_pl": realized_pl,
        "closed_trades": closed_trades,
        "win_rate_pct": (winning_trades / closed_trades * 100) if closed_trades else None,
        "total_trades": len(trades),
    }
