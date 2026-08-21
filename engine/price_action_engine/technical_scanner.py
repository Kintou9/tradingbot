"""
Technical Setup & Entry/Exit Scanner — Section 11b of the requirements doc.
Combines computed indicators (indicators.py) with an LLM narrative pass
for entry/stop/target suggestions. The LLM never invents indicator values
here — they're computed first and passed in as real numbers.
"""

from anthropic import Anthropic
import os
from dotenv import load_dotenv
from .indicators import compute_indicators, find_support_resistance
from ..llm_json import call_and_extract_json

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

SCANNER_PROMPT_TEMPLATE = """You are a professional technical trader. Using ONLY the
computed indicator values provided below (do not estimate or assume any values not
given — say "not enough data" rather than inventing a number), assess the setup for
{ticker}.

Computed data:
{indicator_summary}

Cover each of these 8 points, in order, briefly:
1. Trend direction — price relative to the 50DMA and 200DMA.
2. Support & resistance — the horizontal levels given above; note any Fibonacci
   retracement levels only if they can be derived from the provided price data.
3. Volume profile — any notable recent volume spikes, if volume data is given.
4. RSI(14) signal — overbought (>70), oversold (<30), or neutral, and what that
   implies for momentum.
5. MACD signal — bullish/bearish crossover state, if MACD values are given.
6. Bollinger Bands signal — price position relative to the bands, if given.
7. Proposed entry range and stop-loss level.
8. Two price targets (target_1, target_2), the resulting reward:risk ratio, and a
   probability-of-success estimate (0-100) for the trade reaching target_1 before
   the stop-loss.

Your response MUST end with this exact JSON block and nothing after it — this is
required output, not optional:
{{"ticker": "...", "trend": "up|down|range", "entry_price": 0.0,
  "stop_loss": 0.0, "target_1": 0.0, "target_2": 0.0,
  "reward_risk_ratio": 0.0, "probability_estimate": 0}}
"""


def run_technical_scan(ticker: str, ohlcv_df) -> dict:
    df = compute_indicators(ohlcv_df)
    levels = find_support_resistance(df)
    latest = df.iloc[-1]

    indicator_summary = f"""
    Close: {latest['close']}
    SMA50: {latest.get('sma_50')}, SMA200: {latest.get('sma_200')}
    RSI(14): {latest.get('rsi_14')}
    Support: {levels['support']}, Resistance: {levels['resistance']}
    """

    prompt = SCANNER_PROMPT_TEMPLATE.format(ticker=ticker, indicator_summary=indicator_summary)

    raw_text, structured = call_and_extract_json(client, "claude-sonnet-5", prompt, max_tokens=2500)
    return {"raw_output": raw_text, "computed_levels": levels, "structured": structured}
