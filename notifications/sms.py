"""
Twilio SMS notifications — Section 9 of the requirements doc.

Triggers implemented:
- Every real trade execution (buy/sell) — so you know what the bot did
  without having to check the dashboard.
- Automatic kill-switch engagement (e.g. daily loss limit breach) — a
  safety event, sent immediately.
- Unhandled crashes in either scheduled job (after-hours research,
  market-hours trading) — a separate alert channel for bot failures,
  not just trading news, per Section 9's explicit requirement.
- A one-line summary at the end of each after-hours research run.

Notification failures never crash the caller — a bad Twilio config or a
network hiccup shouldn't take down the trading loop. Errors are printed,
not raised.
"""

import os
import traceback

from dotenv import load_dotenv
from twilio.rest import Client

load_dotenv()

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")
TWILIO_FROM_NUMBER = os.getenv("TWILIO_FROM_NUMBER")
TWILIO_TO_NUMBER = os.getenv("TWILIO_TO_NUMBER")


def send_sms(body: str) -> None:
    if not all([TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_FROM_NUMBER, TWILIO_TO_NUMBER]):
        print(f"[notifications] Twilio not configured, skipping SMS: {body}")
        return

    try:
        client = Client(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN)
        client.messages.create(body=body, from_=TWILIO_FROM_NUMBER, to=TWILIO_TO_NUMBER)
    except Exception as exc:
        print(f"[notifications] Failed to send SMS: {exc}")


def notify_trade(ticker: str, action: str, quantity: float, price: float, reason: str) -> None:
    send_sms(f"Trading Bot: {action.upper()} {quantity} {ticker} @ ${price:.2f} ({reason})")


def notify_kill_switch_engaged(reason: str) -> None:
    send_sms(f"Trading Bot ALERT: kill switch auto-engaged ({reason}). Trading halted.")


def notify_error(context: str, exc: Exception) -> None:
    send_sms(f"Trading Bot ERROR in {context}: {exc}")
    print(f"[notifications] {context} crashed:\n{traceback.format_exc()}")


def notify_daily_summary(tickers_scanned: int, notes_generated: int) -> None:
    send_sms(
        f"Trading Bot: after-hours research complete — {tickers_scanned} tickers scanned, "
        f"{notes_generated} research notes generated."
    )
