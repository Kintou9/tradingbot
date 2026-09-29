"""Research active Watcher candidates and the trading watchlist using real data.

Each provider/module fails independently; successful reports are saved and
partial failures remain visible. This job produces research, never orders.
"""
import json
from datetime import timedelta

from engine.valuation_engine.deep_dive import run_deep_dive
from engine.valuation_engine.dcf import run_dcf
from engine.valuation_engine.fundamentals_data import (
    get_company_name, get_market_data, get_insider_transactions,
    get_recent_13f_filers, get_financial_summary,
)
from engine.price_action_engine.market_data import fetch_ohlcv
from engine.price_action_engine.technical_scanner import run_technical_scan
from engine.news_sentiment.sentiment import fetch_news, score_sentiment
from db.session import SessionLocal
from db.models import NewsItem, ResearchNote, ResearchStatus, WatchedTicker, NotificationOutbox
from db.research_status import utcnow, latest_notes
from db.watcher_retention import expire_watched
from db.execution_lock import execution_lock

WATCHLIST = ["PLTR", "AVGO", "MSFT", "NOW", "CRWD", "AVPT", "DDOG", "SNOW", "CLFD", "AGYS"]
NEWS_LOOKBACK_DAYS = 7


def research_targets(db):
    expire_watched(db)
    watched = [w.ticker for w in db.query(WatchedTicker).order_by(WatchedTicker.added_at, WatchedTicker.id)]
    return list(dict.fromkeys(watched + WATCHLIST))


def _save_note(db, ticker, module, result):
    structured = result["structured"]
    if not isinstance(structured, dict) or not structured:
        raise ValueError("Research provider returned no structured assessment")
    if module == "deep_dive" and structured.get("verdict") not in {"buy", "hold", "avoid", "short"}:
        raise ValueError("Research provider returned an invalid assessment")
    verdict = structured.get("verdict") if module == "deep_dive" else structured.get("trend")
    confidence = structured.get("confidence" if module == "deep_dive" else "probability_estimate")
    db.add(ResearchNote(ticker=ticker, module=module, verdict=verdict,
        confidence=float(confidence or 0) / 100, raw_output=result["raw_output"],
        structured_output=json.dumps(structured)))
    db.commit()


def research_ticker(db, ticker):
    before = latest_notes(db, ticker)["deep_dive"]
    previous_verdict = before.verdict if before else None
    attempt = db.get(ResearchStatus, ticker)
    if attempt is None:
        attempt = ResearchStatus(ticker=ticker)
        db.add(attempt)
    attempt.status, attempt.started_at = "running", utcnow()
    attempt.finished_at, attempt.errors_json = None, None
    db.commit()
    errors, saved = [], []

    def stage(label, fn):
        try:
            return fn()
        except Exception as exc:
            db.rollback()
            # Provider exception payloads can contain URLs/credentials. Show
            # the failing stage and exception type, not raw upstream payloads.
            errors.append(f"{label}: {type(exc).__name__}")
            return None

    def save(module, fn):
        result = fn()
        _save_note(db, ticker, module, result)
        saved.append(module)
        return result

    ohlcv = stage("Price history", lambda: fetch_ohlcv(ticker))
    if ohlcv is not None:
        stage("Technical analysis", lambda: save("technical_scan", lambda: run_technical_scan(ticker, ohlcv_df=ohlcv)))

    def ingest_news():
        today = utcnow().date()
        items = fetch_news(ticker, from_date=(today-timedelta(days=NEWS_LOOKBACK_DAYS)).isoformat(), to_date=today.isoformat())
        for item in items:
            url = item.get("url")
            if url and db.query(NewsItem).filter(NewsItem.ticker == ticker, NewsItem.url == url).first():
                continue
            from datetime import datetime, timezone
            db.add(NewsItem(ticker=ticker, headline=item.get("headline", ""), source=item.get("source"), url=url,
                sentiment_score=score_sentiment(item.get("headline", ""), item.get("summary", "")),
                published_at=datetime.fromtimestamp(item["datetime"], timezone.utc).replace(tzinfo=None) if item.get("datetime") else None))
            db.flush()
        db.commit()
    stage("News sentiment", ingest_news)

    company_name = stage("Company lookup", lambda: get_company_name(ticker))
    if company_name is None:
        errors.append("Fundamentals: company/SEC identifier unavailable")
    else:
        def deep_dive():
            market = get_market_data(ticker)
            if ohlcv is not None:
                market["latest_close"] = float(ohlcv["close"].iloc[-1])
            return run_deep_dive(ticker, market_data=market,
                insider_data=get_insider_transactions(ticker),
                institutional_data=get_recent_13f_filers(ticker, company_name))
        stage("Company assessment", lambda: save("deep_dive", deep_dive))
        stage("Valuation", lambda: save("dcf", lambda: run_dcf(ticker, company_name=company_name,
            filing_context=get_financial_summary(ticker))))

    attempt = db.get(ResearchStatus, ticker)
    attempt.status = "complete" if not errors else ("partial" if saved else "failed")
    attempt.finished_at, attempt.errors_json = utcnow(), json.dumps(errors)
    verdict = latest_notes(db, ticker)["deep_dive"]
    # Alert Watcher users on first assessment or a changed assessment. A
    # partial run must never advertise an old verdict as a newly updated one.
    if "deep_dive" in saved and verdict and verdict.verdict != previous_verdict and db.query(WatchedTicker).filter(WatchedTicker.ticker == ticker).first():
        suffix = " Other research modules need attention; check Watcher." if errors else " Details in Watcher."
        db.add(NotificationOutbox(body=f"Watcher research: {ticker} assessment {verdict.verdict.upper()}" + suffix))
    db.commit()
    return {"ticker": ticker, "status": attempt.status, "notes_generated": len(saved), "errors": errors}


def run_after_hours_research(tickers=None):
    db = SessionLocal()
    try:
        with execution_lock(db, lock_id=731908413):
            active = research_targets(db)
            targets = active if tickers is None else [t for t in dict.fromkeys(tickers) if t in active]
            results = []
            for ticker in targets:
                try:
                    results.append(research_ticker(db, ticker))
                except Exception:
                    db.rollback()
                    # Database failures must propagate; don't mark a batch done
                    # when its state could not be persisted.
                    raise
            return results
    finally:
        db.close()


if __name__ == "__main__":
    print(json.dumps(run_after_hours_research(), indent=2))
