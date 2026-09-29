"""
Alpaca broker wrapper. Official, documented API — recommended default
over the Robinhood route (see requirements doc Section 10 for why).
"""

import os
from dotenv import load_dotenv
from alpaca.trading.client import TradingClient
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import StockLatestQuoteRequest
from alpaca.data.enums import DataFeed
from datetime import datetime, timezone
import math
import requests

load_dotenv()

ALPACA_API_KEY = os.getenv("ALPACA_API_KEY")
ALPACA_SECRET_KEY = os.getenv("ALPACA_SECRET_KEY")
_paper_setting = os.getenv("ALPACA_PAPER", "true").lower()
if _paper_setting not in {"true", "false"}:
    raise ValueError("ALPACA_PAPER must be true or false")
PAPER = _paper_setting == "true"

class _TimeoutSession(requests.Session):
    def request(self, method, url, **kwargs):
        kwargs.setdefault("timeout", (5, 15))
        return super().request(method, url, **kwargs)


def _bounded(client):
    # alpaca-py's installed RESTClient exposes no public timeout option.
    # Keep its request transport bounded so a failed broker cannot hang a worker.
    client._session.close()
    client._session = _TimeoutSession()
    return client


client = _bounded(TradingClient(ALPACA_API_KEY, ALPACA_SECRET_KEY, paper=PAPER))


def get_account():
    return client.get_account()


def get_positions():
    return client.get_all_positions()


def assert_trading_mode(account=None):
    """A mode flag alone can never authorize real-money submissions."""
    if PAPER:
        return
    if os.getenv("LIVE_TRADING_ENABLED", "false").lower() != "true":
        raise ValueError("Live trading is locked: LIVE_TRADING_ENABLED is not true")
    account = account or get_account()
    if not os.getenv("LIVE_ACCOUNT_ID") or str(account.id) != os.getenv("LIVE_ACCOUNT_ID"):
        raise ValueError("Live account does not match LIVE_ACCOUNT_ID")


def _submit_order(order):
    """Internal transport. Callers must use engine.entry_exit.execution."""
    assert_trading_mode()
    return client.submit_order(order)


def fresh_ask(ticker):
    data = _bounded(StockHistoricalDataClient(ALPACA_API_KEY, ALPACA_SECRET_KEY))
    quote = data.get_stock_latest_quote(StockLatestQuoteRequest(
        symbol_or_symbols=ticker, feed=DataFeed(os.getenv("ALPACA_DATA_FEED", "iex"))))[ticker]
    age = (datetime.now(timezone.utc) - quote.timestamp).total_seconds()
    price = float(quote.ask_price)
    if age < -5 or age > 60 or not math.isfinite(price) or price <= 0:
        raise ValueError(f"{ticker}: missing or stale executable quote")
    return price
