"""
Weekly stock discovery — surfaces new tickers onto the personal Watcher
list (db.models.WatchedTicker), one run per week, intended to fire every
Friday after the market closes (see functions/weekly_discovery.py for the
scheduling wrapper).

What this is NOT: a live index-constituent feed. There's no wired-up
S&P 500 / Nasdaq / Dow constituents API in this project, so each category
below is a hand-maintained list of real companies that plausibly belong
to it — it can drift from the live index membership over time and isn't
re-verified against SEC/exchange data. What IS real and live: market cap
(Finnhub) and realized volatility (computed from actual Twelve Data OHLCV
bars), which drive the small-cap / low-vol / high-vol picks and get
recorded in each ticker's note so the pick is checkable, not asserted.

Discovery adds candidates to the Watcher. The separate research worker then
picks up missing/stale reports in bounded batches; discovery never trades.
"""

import math
import time
from datetime import date, datetime, timezone

from db.session import SessionLocal
from db.models import DiscoveryRunLog, Position, WatchedTicker
from db.watcher_retention import AUTO_DISCOVERY_PREFIX, expire_watched
from broker.alpaca_client import get_positions
from engine.price_action_engine.market_data import fetch_ohlcv
from engine.valuation_engine.fundamentals_data import get_market_data, get_company_name
from functions.market_hours_trading import WATCHLIST

# Hand-maintained candidate pools, real tickers, roughly grouped by theme.
# Order matters only as a tie-break (first not-yet-excluded wins the slot).
CATEGORY_POOLS = {
    "oil industry": ["XOM", "CVX", "COP", "OXY", "SLB", "HAL", "MPC", "VLO", "PSX", "EOG"],
    "tech industry": ["ORCL", "IBM", "ADBE", "CSCO", "INTU", "TXN", "QCOM", "AMD", "MU", "PANW"],
    "energy industry": ["NEE", "DUK", "SO", "AEP", "EXC", "SRE", "D", "ED", "CEG", "VST"],
    "nasdaq": ["ASML", "BKNG", "ADI", "LRCX", "KLAC", "MRVL", "FTNT", "CTAS", "ODFL", "PAYX"],
    "dow jones": ["JPM", "V", "HD", "MCD", "CAT", "BA", "DIS", "WMT", "PG", "UNH"],
    "s&p 500": ["ABBV", "PFE", "TMO", "DHR", "LIN", "RTX", "LMT", "GE", "UPS", "LOW"],
    "small-cap": ["FIVE", "SMPL", "CVCO", "ATKR", "SITE", "MLI", "POWL", "CALM", "SHOO", "BOOT"],
}

# Number of leftover candidates (after the one-per-category picks above)
# to pull real OHLCV for, to find the low/high-volatility and momentum
# slots. Bounded on purpose — Twelve Data's free tier is rate-limited,
# and this is a once-a-week job, not a live scanner.
VOLATILITY_SHORTLIST_SIZE = 20
OHLCV_CALL_PACING_SECONDS = 2.0


def _already_tracked(db) -> set[str]:
    held = {p.symbol for p in get_positions()}
    watched = {w.ticker for w in db.query(WatchedTicker).all()}
    # Avoid immediately selecting the same batch that just aged out. Keep
    # the two most recent discovery batches excluded even after cleanup.
    recent_runs = db.query(DiscoveryRunLog).order_by(DiscoveryRunLog.id.desc()).limit(2).all()
    recent = {ticker.strip() for run in recent_runs for ticker in (run.tickers or "").split(",") if ticker.strip()}
    return held | watched | recent | set(WATCHLIST)


def _annualized_volatility(ticker: str) -> float | None:
    """Realized volatility from actual daily closes — stdev of the last
    20 daily returns, annualized. None (not 0, not guessed) if the data
    isn't available, so a fetch failure never masquerades as "low vol"."""
    try:
        df = fetch_ohlcv(ticker, outputsize=25)
    except Exception:
        return None
    closes = df["close"]
    if len(closes) < 11:
        return None
    returns = closes.pct_change().dropna().tail(20)
    if len(returns) < 10:
        return None
    stdev = returns.std()
    if stdev is None or math.isnan(stdev):
        return None
    return float(stdev * math.sqrt(252))


def _market_cap_note(ticker: str) -> str:
    try:
        cap = get_market_data(ticker).get("market_cap_millions")
    except Exception:
        cap = None
    return f"mkt cap ${cap / 1000:.1f}B" if cap else "mkt cap data not available"


def run_weekly_discovery() -> list[dict]:
    db = SessionLocal()
    try:
        expire_watched(db)
        excluded = _already_tracked(db)
        picks: list[dict] = []  # {ticker, category, note}
        picked_tickers: set[str] = set()

        # 1. One pick per named category — deterministic, no live calls yet.
        for category, pool in CATEGORY_POOLS.items():
            for ticker in pool:
                if ticker in excluded or ticker in picked_tickers:
                    continue
                picks.append({"ticker": ticker, "category": category, "vol": None})
                picked_tickers.add(ticker)
                break  # category slot filled (or left empty if pool exhausted)

        # 2. Build a shortlist of remaining, untouched candidates and pull
        # real volatility for them — this is what makes the low/high-vol
        # and momentum picks actual data rather than a guess.
        leftover = []
        for pool in CATEGORY_POOLS.values():
            for ticker in pool:
                if ticker not in excluded and ticker not in picked_tickers and ticker not in leftover:
                    leftover.append(ticker)
        shortlist = leftover[:VOLATILITY_SHORTLIST_SIZE]

        vol_by_ticker = {}
        for i, ticker in enumerate(shortlist):
            vol = _annualized_volatility(ticker)
            if vol is not None:
                vol_by_ticker[ticker] = vol
            if i < len(shortlist) - 1:
                time.sleep(OHLCV_CALL_PACING_SECONDS)

        if vol_by_ticker:
            low_vol_ticker = min(vol_by_ticker, key=vol_by_ticker.get)
            picks.append({"ticker": low_vol_ticker, "category": "low volatility", "vol": vol_by_ticker[low_vol_ticker]})
            picked_tickers.add(low_vol_ticker)
            del vol_by_ticker[low_vol_ticker]

        if vol_by_ticker:
            high_vol_ticker = max(vol_by_ticker, key=vol_by_ticker.get)
            picks.append({"ticker": high_vol_ticker, "category": "high volatility", "vol": vol_by_ticker[high_vol_ticker]})
            picked_tickers.add(high_vol_ticker)
            del vol_by_ticker[high_vol_ticker]

        # 3. Wildcard/momentum slot — next-most-volatile of what's left,
        # framed as a momentum name rather than a duplicate vol pick.
        if vol_by_ticker:
            momentum_ticker = max(vol_by_ticker, key=vol_by_ticker.get)
            picks.append({"ticker": momentum_ticker, "category": "momentum", "vol": vol_by_ticker[momentum_ticker]})
            picked_tickers.add(momentum_ticker)

        picks = picks[:10]

        added = []
        for p in picks:
            ticker = p["ticker"]
            try:
                company_name = get_company_name(ticker)
            except Exception:
                company_name = None
            vol_note = f", 20d volatility {p['vol'] * 100:.0f}% (annualized)" if p["vol"] is not None else ""
            note = f"{AUTO_DISCOVERY_PREFIX}{date.today().isoformat()} — {p['category']}; {_market_cap_note(ticker)}{vol_note}"

            if db.query(WatchedTicker).filter(WatchedTicker.ticker == ticker).first():
                continue  # picked twice across runs due to a race — skip rather than error
            db.add(WatchedTicker(ticker=ticker, notes=note))
            added.append({"ticker": ticker, "company_name": company_name, "category": p["category"], "notes": note})

        db.commit()

        db.add(DiscoveryRunLog(
            run_date=date.today(),
            tickers=", ".join(a["ticker"] for a in added) or None,
            timestamp=datetime.now(timezone.utc),
        ))
        db.commit()

        return added
    finally:
        db.close()
