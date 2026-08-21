"""
After-hours research job — intended as an Azure Function with a Timer
trigger (Section 8). Runs the LLM analyst modules + news sentiment for
each ticker in the watchlist, caches results to PostgreSQL.

To deploy as an actual Azure Function, wrap this in the Azure Functions
Python programming model (function_app.py with @app.timer_trigger).
This file holds the logic; wiring it to Azure's trigger decorators is
a separate step once you're ready to deploy.
"""

import json
from datetime import datetime, timedelta, timezone

from engine.valuation_engine.deep_dive import run_deep_dive
from engine.valuation_engine.dcf import run_dcf
from engine.valuation_engine.fundamentals_data import (
    get_company_name,
    get_market_data,
    get_insider_transactions,
    get_recent_13f_filers,
    get_financial_summary,
)
from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.technical_scanner import run_technical_scan
from engine.news_sentiment.sentiment import fetch_news, score_sentiment
from db.session import SessionLocal
from db.models import NewsItem, ResearchNote
from notifications.sms import notify_error, notify_daily_summary

WATCHLIST = ["PLTR", "AVGO", "MSFT", "NOW", "CRWD", "AVPT", "DDOG", "SNOW", "CLFD", "AGYS"]
NEWS_LOOKBACK_DAYS = 7


def run_after_hours_research():
    try:
        notes_generated = _run_research_cycle()
    except Exception as exc:
        notify_error("after_hours_research", exc)
        raise
    notify_daily_summary(len(WATCHLIST), notes_generated)


def _run_research_cycle() -> int:
    today = datetime.now(timezone.utc).date()
    from_date = (today - timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat()
    to_date = today.isoformat()
    notes_generated = 0

    db = SessionLocal()
    try:
        for ticker in WATCHLIST:
            try:
                ohlcv_df = fetch_ohlcv(ticker)
            except Exception as exc:
                print(f"Skipping technical scan for {ticker}: OHLCV fetch failed ({exc})")
                ohlcv_df = None

            news = fetch_news(ticker, from_date=from_date, to_date=to_date)
            for item in news:
                url = item.get("url")
                already_seen = (
                    url and db.query(NewsItem).filter(NewsItem.ticker == ticker, NewsItem.url == url).first()
                )
                if already_seen:
                    continue

                score = score_sentiment(item.get("headline", ""), item.get("summary", ""))
                published_at = (
                    datetime.fromtimestamp(item["datetime"], tz=timezone.utc)
                    if item.get("datetime")
                    else None
                )
                db.add(NewsItem(
                    ticker=ticker,
                    headline=item.get("headline", ""),
                    source=item.get("source"),
                    url=item.get("url"),
                    sentiment_score=score,
                    published_at=published_at,
                ))

            if ohlcv_df is not None:
                technical_result = run_technical_scan(ticker, ohlcv_df=ohlcv_df)
                structured = technical_result["structured"]
                db.add(ResearchNote(
                    ticker=ticker,
                    module="technical_scan",
                    verdict=structured.get("trend"),
                    confidence=structured.get("probability_estimate", 0) / 100,
                    raw_output=technical_result["raw_output"],
                    structured_output=json.dumps(structured),
                ))
                notes_generated += 1

            company_name = get_company_name(ticker)
            if company_name is None:
                print(f"Skipping deep_dive/dcf for {ticker}: no SEC CIK found for this ticker")
                db.commit()
                continue

            try:
                market_data = get_market_data(ticker)
                if ohlcv_df is not None:
                    market_data["latest_close"] = float(ohlcv_df["close"].iloc[-1])
                insider_data = get_insider_transactions(ticker)
                institutional_data = get_recent_13f_filers(ticker, company_name)

                deep_dive_result = run_deep_dive(
                    ticker,
                    market_data=market_data,
                    insider_data=insider_data,
                    institutional_data=institutional_data,
                )
                structured = deep_dive_result["structured"]
                db.add(ResearchNote(
                    ticker=ticker,
                    module="deep_dive",
                    verdict=structured.get("verdict"),
                    confidence=(structured.get("confidence") or 0) / 100,
                    raw_output=deep_dive_result["raw_output"],
                    structured_output=json.dumps(structured),
                ))
                notes_generated += 1

                filing_context = get_financial_summary(ticker)
                dcf_result = run_dcf(ticker, company_name=company_name, filing_context=filing_context)
                structured = dcf_result["structured"]
                db.add(ResearchNote(
                    ticker=ticker,
                    module="dcf",
                    raw_output=dcf_result["raw_output"],
                    structured_output=json.dumps(structured),
                ))
                notes_generated += 1
            except Exception as exc:
                print(f"Skipping deep_dive/dcf for {ticker}: {exc}")

            db.commit()

        print(f"After-hours research complete for {len(WATCHLIST)} tickers.")
        return notes_generated
    finally:
        db.close()


if __name__ == "__main__":
    run_after_hours_research()
