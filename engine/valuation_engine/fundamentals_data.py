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


def get_annual_filings(ticker: str) -> list[dict]:
    """
    Real per-fiscal-year revenue/net income/operating cash flow from SEC
    EDGAR's structured XBRL company facts, each tagged with the actual
    date the 10-K was *filed* — not just its fiscal period end.

    That distinction matters for backtesting: a FY2024 10-K isn't public
    knowledge on 2024-12-31, it's public whenever SEC EDGAR says it was
    filed (typically 60-90 days later). Point-in-time correctness means
    gating on `filed`, never on `fiscal_year_end`.
    """
    cik = get_cik(ticker)
    if cik is None:
        return []

    resp = requests.get(
        f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json",
        headers=SEC_HEADERS,
    )
    if resp.status_code != 200:
        return []

    all_facts = resp.json().get("facts", {})
    facts = all_facts.get("us-gaap", {})
    dei_facts = all_facts.get("dei", {})

    def is_genuinely_annual(e: dict) -> bool:
        """form=="10-K" and fp=="FY" alone isn't reliable — some filers'
        raw XBRL mistags quarterly/comparative datapoints with fp="FY"
        (observed on AVGO: 18 "annual" entries, most of them duration-1
        quarters). Require the (start, end) span to actually be ~a year."""
        try:
            start = date.fromisoformat(e["start"])
            end = date.fromisoformat(e["end"])
        except (KeyError, ValueError):
            return False
        return 340 <= (end - start).days <= 380

    def annual_entries(tag_candidates: list[str], unit: str = "USD") -> list[dict]:
        for tag in tag_candidates:
            entries = facts.get(tag, {}).get("units", {}).get(unit, [])
            annual = [
                e for e in entries
                if e.get("form") == "10-K" and e.get("fp") == "FY" and is_genuinely_annual(e)
            ]
            if annual:
                return annual
        return []

    def instant_shares_by_end(tag_candidates: list[str]) -> dict:
        """Balance-sheet share counts are instantaneous (no `start`), so
        is_genuinely_annual can't vet them — match them to a fiscal-year
        end date directly instead. Used only as a fallback when the
        weighted-average diluted count (the DCF's preferred denominator)
        isn't tagged."""
        for tag in tag_candidates:
            entries = dei_facts.get(tag, {}).get("units", {}).get("shares", []) \
                or facts.get(tag, {}).get("units", {}).get("shares", [])
            by_end = {}
            for e in entries:
                if e.get("form") == "10-K" and e.get("end"):
                    # keep the value from the earliest filing that reported
                    # this period end — that's the point-in-time-correct one
                    prev = by_end.get(e["end"])
                    if prev is None or e.get("filed", "") < prev.get("filed", ""):
                        by_end[e["end"]] = e
            if by_end:
                return {end: e["val"] for end, e in by_end.items()}
        return {}

    revenue_entries = annual_entries([
        "RevenueFromContractWithCustomerExcludingAssessedTax",
        "RevenueFromContractWithCustomerIncludingAssessedTax",
        "Revenues",
        "SalesRevenueNet",
    ])
    net_income_by_end = {e["end"]: e["val"] for e in annual_entries(["NetIncomeLoss"])}
    op_cash_flow_by_end = {e["end"]: e["val"] for e in annual_entries(["NetCashProvidedByUsedInOperatingActivities"])}

    # Diluted share count — the denominator the DCF needs to turn equity
    # value into fair value per share. Without this in the filing context
    # the LLM guesses it from memory, and a stale guess (splits, buybacks,
    # dilution) throws the per-share output off by multiples. Prefer the
    # weighted-average diluted figure from the income statement; fall back
    # to basic, then to a balance-sheet / cover-page shares-outstanding
    # count.
    diluted_shares_by_end = {
        e["end"]: e["val"]
        for e in annual_entries(["WeightedAverageNumberOfDilutedSharesOutstanding"], unit="shares")
    }
    if not diluted_shares_by_end:
        diluted_shares_by_end = {
            e["end"]: e["val"]
            for e in annual_entries(["WeightedAverageNumberOfSharesOutstandingBasic"], unit="shares")
        }
    if not diluted_shares_by_end:
        diluted_shares_by_end = instant_shares_by_end([
            "CommonStockSharesOutstanding",
            "EntityCommonStockSharesOutstanding",
        ])

    filings = []
    seen_ends = set()
    for e in sorted(revenue_entries, key=lambda x: x["end"]):
        if e["end"] in seen_ends:
            continue
        seen_ends.add(e["end"])
        filings.append({
            "fiscal_year_end": e["end"],
            "filed": e["filed"],
            "revenue": e["val"],
            "net_income": net_income_by_end.get(e["end"]),
            "operating_cash_flow": op_cash_flow_by_end.get(e["end"]),
            "diluted_shares": diluted_shares_by_end.get(e["end"]),
        })

    # A single 10-K often discloses 2-3 years of comparative figures, so
    # multiple fiscal years can legitimately share the same filed date.
    # Only the most recent fiscal year per filing is a new "as of" point —
    # the older ones don't change what get_financial_summary_asof returns
    # for that date, so keep just one to avoid redundant DCF calls.
    latest_per_filed = {}
    for f in filings:
        existing = latest_per_filed.get(f["filed"])
        if existing is None or f["fiscal_year_end"] > existing["fiscal_year_end"]:
            latest_per_filed[f["filed"]] = f

    return sorted(latest_per_filed.values(), key=lambda f: f["filed"])


def _format_filing_summary(filings: list[dict]) -> str:
    lines = []
    for f in filings:
        fy = f["fiscal_year_end"][:4]
        line = f"FY{fy}: Revenue ${f['revenue']:,.0f}"
        if f["net_income"] is not None:
            line += f", Net Income ${f['net_income']:,.0f}"
        if f["operating_cash_flow"] is not None:
            line += f", Operating Cash Flow ${f['operating_cash_flow']:,.0f}"
        if f.get("diluted_shares") is not None:
            line += f", Diluted Shares Outstanding {f['diluted_shares']:,.0f}"
        lines.append(line)
    return "\n".join(lines)


def get_financial_summary(ticker: str, years: int = 3) -> str:
    """
    Real revenue/net income/operating cash flow for the most recent N
    fiscal years — used as the DCF module's filing_context so it
    projects from real filed numbers rather than text scraped out of a
    10-K. For live/current-day use; see get_financial_summary_asof for
    point-in-time (backtesting) use.
    """
    filings = get_annual_filings(ticker)
    if not filings:
        return "No structured revenue data found in SEC filings for this ticker."
    return _format_filing_summary(filings[-years:])


def get_financial_summary_asof(ticker: str, as_of: str, years: int = 3) -> str:
    """
    Same as get_financial_summary, but restricted to filings that were
    actually public by `as_of` (an ISO date string) — i.e. filed <=
    as_of. This is what makes point-in-time backtesting honest: on any
    given historical date, the DCF only sees what a trader actually
    could have seen that day, never a future filing.
    """
    filings = [f for f in get_annual_filings(ticker) if f["filed"] <= as_of]
    if not filings:
        return "No SEC filings available as of this date."
    return _format_filing_summary(filings[-years:])
