"""Validated server-side limits. Missing/invalid account data blocks entries."""
from dataclasses import dataclass
from datetime import datetime, time, timezone
from decimal import Decimal, ROUND_DOWN
import math
import os
from zoneinfo import ZoneInfo

ET = ZoneInfo("America/New_York")


class RiskRejected(ValueError):
    pass


def positive(value, name):
    try:
        result = float(value)
    except (TypeError, ValueError) as exc:
        raise RiskRejected(f"{name} must be a finite positive number") from exc
    if not math.isfinite(result) or result <= 0:
        raise RiskRejected(f"{name} must be a finite positive number")
    return result


@dataclass(frozen=True)
class Limits:
    max_trade: float
    max_entries: int
    max_exposure_pct: float
    max_position_pct: float
    daily_loss_pct: float

    @classmethod
    def from_env(cls):
        trades = positive(os.getenv("MAX_TRADES_PER_DAY", "3"), "MAX_TRADES_PER_DAY")
        if not trades.is_integer():
            raise RiskRejected("MAX_TRADES_PER_DAY must be an integer")
        percentages = [positive(os.getenv(key, default), key) for key, default in (
            ("MAX_TOTAL_EXPOSURE_PCT", "0.30"), ("MAX_POSITION_PCT", "0.10"),
            ("DAILY_LOSS_LIMIT_PCT", "0.02"))]
        if any(p > 1 for p in percentages):
            raise RiskRejected("Risk percentages must be greater than zero and at most 1")
        return cls(positive(os.getenv("MAX_TRADE_NOTIONAL", "100"), "MAX_TRADE_NOTIONAL"),
                   int(trades), *percentages)


def day_start_utc(now=None):
    now = now or datetime.now(timezone.utc)
    return datetime.combine(now.astimezone(ET).date(), time.min, ET).astimezone(timezone.utc).replace(tzinfo=None)


def loss_breached(account, limits):
    previous = positive(account.last_equity, "Previous equity")
    equity = float(account.equity)
    if not math.isfinite(equity):
        raise RiskRejected("Invalid account equity")
    return (previous - equity) / previous >= limits.daily_loss_pct


def price_tick(value):
    number = Decimal(str(positive(value, "Price")))
    return float(number.quantize(Decimal("0.01") if number >= 1 else Decimal("0.0001"), rounding=ROUND_DOWN))
