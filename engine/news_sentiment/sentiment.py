"""
News sentiment scoring — pulls headlines from Finnhub/Benzinga and scores
them via the Claude API rather than simple keyword matching (Section 6).
"""

from anthropic import Anthropic
import os
import requests
from dotenv import load_dotenv

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))
FINNHUB_API_KEY = os.getenv("FINNHUB_API_KEY")


def fetch_news(ticker: str, from_date: str, to_date: str) -> list[dict]:
    """Pull recent headlines for a ticker from Finnhub."""
    url = "https://finnhub.io/api/v1/company-news"
    params = {"symbol": ticker, "from": from_date, "to": to_date, "token": FINNHUB_API_KEY}
    resp = requests.get(url, params=params)
    resp.raise_for_status()
    return resp.json()


def score_sentiment(headline: str, summary: str = "") -> float:
    """Returns a sentiment score from -1.0 (very bearish) to 1.0 (very bullish)."""
    prompt = f"""Rate the sentiment of this financial news headline for the
stock it concerns, on a scale from -1.0 (very bearish) to 1.0 (very bullish).
Respond with ONLY the numeric score, nothing else.

Headline: {headline}
Summary: {summary}
"""
    response = client.messages.create(
        model="claude-sonnet-5",
        max_tokens=10,
        messages=[{"role": "user", "content": prompt}],
    )
    text_blocks = [block.text for block in response.content if block.type == "text"]
    if not text_blocks:
        return 0.0
    try:
        return float(text_blocks[0].strip())
    except ValueError:
        return 0.0
