"""
Real fundamentals data for the LLM analyst modules (dcf.py, deep_dive.py) —
SEC EDGAR for financial statements and 13F institutional filings, Finnhub
for insider transactions and valuation multiples.

Section 11a's institutional-activity caveat applies: 13Fs are filed
quarterly with a lag, so "last 6 months" really means the two most
recently filed quarters. SEC's full-text search also only surfaces filer
names/dates, not per-holding share-count deltas — reading those out
requires parsing each 13F's individual XML holdings table, which isn't
done here. Nothing in this module is fabricated; a data point that isn't
available is simply omitted so the prompts' "say data not available"
instruction can do its job honestly.
"""

import os
from datetime import date, timedelta

import requests
from dotenv import load_dotenv

load_dotenv()

FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")

# SEC requires a descriptive User-Agent identifying the requester on all
# data.sec.gov / www.sec.gov API calls, or it returns 403.
SEC_HEADERS = {"User-Agent": "trading-bot-research contact@example.com"}

_TICKER_MAP_CACHE = None


def _ticker_map() -> dict:
    global _TICKER_MAP_CACHE
    if _TICKER_MAP_CACHE is None:
        resp = requests.get("https://www.sec.gov/files/company_tickers.json", headers=SEC_HEADERS)
        resp.raise_for_status()
        _TICKER_MAP_CACHE = {
            v["ticker"]: {"cik": v["cik_str"], "title": v["title"]} for v in resp.json().values()
        }
    return _TICKER_MAP_CACHE


def get_cik(ticker: str) -> int | None:
    entry = _ticker_map().get(ticker.upper())
    return entry["cik"] if entry else None


def get_company_name(ticker: str) -> str | None:
    entry = _ticker_map().get(ticker.upper())
    return entry["title"] if entry else None


def get_market_data(ticker: str) -> dict:
    """Market cap, 52-week range, and valuation multiples from Finnhub."""
    resp = requests.get(
        "https://finnhub.io/api/v1/stock/metric",
        params={"symbol": ticker, "metric": "all", "token": FINNHUB_API_KEY},
    )
    resp.raise_for_status()
    metric = resp.json().get("metric", {})
    return {
        "market_cap_millions": metric.get("marketCapitalization"),
        "52_week_high": metric.get("52WeekHigh"),
        "52_week_low": metric.get("52WeekLow"),
        "pe_ttm": metric.get("peBasicExclExtraTTM"),
        "pb": metric.get("pbAnnual"),
        "revenue_growth_ttm_yoy_pct": metric.get("revenueGrowthTTMYoy"),
    }


def get_insider_transactions(ticker: str, limit: int = 10) -> list[dict]:
    """Recent Form 4 insider transactions from Finnhub."""
    resp = requests.get(
        "https://finnhub.io/api/v1/stock/insider-transactions",
        params={"symbol": ticker, "token": FINNHUB_API_KEY},
    )
    resp.raise_for_status()
    data = resp.json().get("data", [])
    return [
        {
            "name": t.get("name"),
            "change": t.get("change"),
            "transaction_date": t.get("transactionDate"),
            "transaction_code": t.get("transactionCode"),  # e.g. "S" sell, "P" purchase
        }
        for t in data[:limit]
    ]


def get_recent_13f_filers(ticker: str, company_name: str, limit: int = 10) -> list[dict]:
    """
    Institutions whose 13F-HR filings mention this company, restricted to
    the last ~7 months so this actually reflects the two most recently
    filed quarters (per Section 11a's lag caveat) rather than SEC's
    relevance-ranked search, which otherwise mixes in filings years old.
    """
    today = date.today()
    resp = requests.get(
        "https://efts.sec.gov/LATEST/search-index",
        params={
            "q": f'"{company_name}"',
            "forms": "13F-HR",
            "dateRange": "custom",
            "startdt": (today - timedelta(days=210)).isoformat(),
            "enddt": today.isoformat(),
        },
        headers=SEC_HEADERS,
    )
    resp.raise_for_status()
    hits = resp.json().get("hits", {}).get("hits", [])

    seen = set()
    filers = []
    for hit in sorted(hits, key=lambda h: h.get("_source", {}).get("file_date", ""), reverse=True):
        source = hit.get("_source", {})
        names = source.get("display_names", [])
        name = names[0] if names else None
        if not name or name in seen:
            continue
        seen.add(name)
        filers.append({
            "filer": name,
            "filed": source.get("file_date"),
            "period_ending": source.get("period_ending"),
        })
        if len(filers) >= limit:
            break
    return filers


def get_financial_summary(ticker: str, years: int = 3) -> str:
    """
    Real revenue/net income/operating cash flow for the last N fiscal
    years, pulled from SEC EDGAR's structured XBRL company facts — used as
    the DCF module's filing_context so it projects from real filed
    numbers rather than text scraped out of a 10-K.
    """
    cik = get_cik(ticker)
    if cik is None:
        return "No SEC filing data found for this ticker."

    resp = requests.get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        headers=SEC_HEADERS,
    )
    if resp.status_code != 200:
        return "No SEC filing data found for this ticker."

    facts = resp.json().get("facts", {}).get("us-gaap", {})

    def annual_series(tag_candidates: list[str]) -> dict:
        for tag in tag_candidates:
            entries = facts.get(tag, {}).get("units", {}).get("USD", [])
            annual = {e["end"]: e["val"] for e in entries if e.get("form") == "10-K" and e.get("fp") == "FY"}
            if annual:
                return annual
        return {}

    revenue = annual_series(["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"])
    net_income = annual_series(["NetIncomeLoss"])
    op_cash_flow = annual_series(["NetCashProvidedByUsedInOperatingActivities"])

    years_available = sorted(revenue.keys())[-years:]
    if not years_available:
        return "No structured revenue data found in SEC filings for this ticker."

    lines = []
    for end_date in years_available:
        fy = end_date[:4]
        line = f"FY{fy}: Revenue ${revenue[end_date]:,.0f}"
        if end_date in net_income:
            line += f", Net Income ${net_income[end_date]:,.0f}"
        if end_date in op_cash_flow:
            line += f", Operating Cash Flow ${op_cash_flow[end_date]:,.0f}"
        lines.append(line)

    return "\n".join(lines)
