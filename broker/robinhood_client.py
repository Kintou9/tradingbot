"""
Robinhood broker wrapper via robin_stocks. Unofficial API — see requirements
doc Section 10 for the ToS/reliability tradeoffs before relying on this
for anything beyond a personal small-scale bot.

Credentials + TOTP secret come from .env / Azure Key Vault — never hardcode.
"""

import os
import pyotp
import robin_stocks.robinhood as rh

ROBINHOOD_USERNAME = os.getenv("ROBINHOOD_USERNAME")
ROBINHOOD_PASSWORD = os.getenv("ROBINHOOD_PASSWORD")
ROBINHOOD_TOTP_SECRET = os.getenv("ROBINHOOD_TOTP_SECRET")

_logged_in = False


def login():
    global _logged_in
    if _logged_in:
        return
    totp = pyotp.TOTP(ROBINHOOD_TOTP_SECRET).now()
    rh.login(ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD, mfa_code=totp, store_session=True)
    _logged_in = True


def get_positions():
    login()
    return rh.account.build_holdings()


def place_market_order(ticker: str, qty: float, side: str):
    """side: 'buy' or 'sell'. Note: no confirmation prompts like the app —
    your risk management logic (Section 7) is the only thing preventing
    a bad order here."""
    login()
    if side == "buy":
        return rh.orders.order_buy_fractional_by_quantity(ticker, qty)
    return rh.orders.order_sell_fractional_by_quantity(ticker, qty)
