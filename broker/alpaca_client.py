"""
Alpaca broker wrapper. Official, documented API — recommended default
over the Robinhood route (see requirements doc Section 10 for why).
"""

import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.trading.requests import MarketOrderRequest
from alpaca.trading.enums import OrderSide, TimeInForce

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
PAPER = os.getenv("ALPACA_PAPER", "true").lower() == "true"

client = TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER)


def get_account():
    return client.get_account()


def get_positions():
    return client.get_all_positions()


def place_market_order(ticker: str, qty: float, side: str):
    """side: 'buy' or 'sell'. Supports fractional qty."""
    order = MarketOrderRequest(
        symbol=ticker,
        qty=qty,
        side=OrderSide.BUY if side == "buy" else OrderSide.SELL,
        time_in_force=TimeInForce.DAY,
    )
    return client.submit_order(order)
