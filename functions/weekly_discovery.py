"""
Weekly stock-discovery job — intended to fire every Friday after market
close (Section 8 style: a Timer-triggered job, same shape as
after_hours_research.py and market_hours_trading.py). Surfaces new
tickers onto the personal Watcher list from a handful of sector/theme
pools plus real computed volatility — see engine/discovery/stock_discovery.py
for what "discovery" actually means here and its honesty caveats.

Scheduling for this specific job currently lives in api/main.py's startup
background task (checks day-of-week + time-of-day + "already ran this
week" via DiscoveryRunLog) rather than a standalone Azure Function Timer
trigger, for the same reason the daily research/trading jobs aren't wired
to one yet: there's no deployed infra for this project to run in
unattended. It only fires while that backend process is alive.
"""

from engine.discovery.stock_discovery import run_weekly_discovery
from notifications.sms import notify_error


def run_weekly_stock_discovery() -> list[dict]:
    try:
        added = run_weekly_discovery()
    except Exception as exc:
        notify_error("weekly_discovery", exc)
        raise
    print(f"Weekly discovery complete: added {len(added)} tickers — {[a['ticker'] for a in added]}")
    return added


if __name__ == "__main__":
    run_weekly_stock_discovery()
