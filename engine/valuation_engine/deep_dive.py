"""
Ticker Deep Dive & Red Flags module — Section 11a of the requirements doc.

Requires real data injected before the LLM call:
- Price/valuation: your OHLCV + fundamentals API (Alpha Vantage / Twelve Data)
- Insider activity: SEC EDGAR Form 4, or Finnhub insider-transactions endpoint
- Institutional activity: 13F filings via SEC EDGAR full-text search
"""

from anthropic import Anthropic
import os
from dotenv import load_dotenv
from ..llm_json import call_and_extract_json

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DEEP_DIVE_PROMPT_TEMPLATE = """You are a hedge fund equity analyst who has screened
thousands of stocks. Run a full snapshot for {ticker} using the data provided below.
Do not fabricate any figures not present in the provided data — say "data not available"
instead.

Cover each of these 8 points, in order, briefly:
1. Price, market cap, and 52-week range.
2. One-line business description and a moat score (0-100).
3. Trailing 4-quarter revenue and EPS growth, plus earnings surprise history.
4. Valuation multiples vs. sector average and the company's own 5-year average.
5. Top bullish catalysts.
6. Top bearish red flags.
7. Insider and institutional activity over the last 6 months.
8. Verdict (buy/hold/avoid/short) with a confidence score (0-100) — this is one
   internal signal for the entry/exit engine, not a standalone recommendation.

Your response MUST end with this exact JSON block and nothing after it — this is
required output, not optional:
{{"ticker": "...", "verdict": "buy|hold|avoid|short", "confidence": 0,
  "moat_score": 0, "top_bullish_catalysts": ["..."], "top_bearish_risks": ["..."]}}
"""


def run_deep_dive(ticker: str, market_data: dict, insider_data: dict, institutional_data: dict) -> dict:
    """
    market_data / insider_data / institutional_data: dicts of real pulled
    data — this is an internal bot signal, not a standalone recommendation.
    The entry/exit engine (engine/entry_exit/signals.py) combines this with
    price action + valuation before anything is gated through risk rules.
    """
    context = f"""
Market data: {market_data}
Insider activity (last 6 months): {insider_data}
Institutional activity (most recent 13F filings): {institutional_data}
"""
    prompt = DEEP_DIVE_PROMPT_TEMPLATE.format(ticker=ticker) + context

    raw_text, structured = call_and_extract_json(client, "claude-sonnet-5", prompt, max_tokens=3000)
    # TODO: store raw_text + structured via ResearchNote
    return {"raw_output": raw_text, "structured": structured}
