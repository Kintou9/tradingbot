"""
DCF valuation module — implements Section 2 / Section 11's DCF prompt template.

Real implementation should:
1. Pull historical financials from SEC EDGAR (or your fundamentals API)
2. Inject that data into the DCF prompt as [FILING_CONTEXT]
3. Call the Anthropic API (see engine/news_sentiment/sentiment.py for
   an example client setup pattern)
4. Parse the structured JSON block from the response
5. Store the result via db.models.ResearchNote
"""

from anthropic import Anthropic
import os
from dotenv import load_dotenv
from ..llm_json import call_and_extract_json

load_dotenv()

client = Anthropic(api_key=os.getenv("ANTHROPIC_API_KEY"))

DCF_PROMPT_TEMPLATE = """You are a senior equity research analyst. Build a discounted
cash flow (DCF) valuation for {company_name} (ticker: {ticker}) over a {n_years}-year
explicit forecast horizon plus a terminal value.

Use ONLY the filing context provided below — do not invent revenue, margin, or
share-count figures not present in it; say "data not available" instead.

The filing context includes "Diluted Shares Outstanding" for each fiscal year.
Use the most recent one as the share count when converting equity value to fair
value per share — do NOT substitute a share count from your own memory. If (and
only if) no diluted share count appears in the filing context, state clearly in
step 5 that the per-share figure is unreliable because the share count is
unavailable, and set "share_count_source" to "unavailable" in the JSON block.

Work through these steps, briefly:
1. Summarize the starting revenue, FCF margin, and growth trajectory from the
   filing context.
2. Project revenue and free cash flow for each of the next {n_years} years,
   stating your growth-rate assumption for each year.
3. Estimate a WACC (discount rate) appropriate for this company's risk profile,
   and state the reasoning.
4. Estimate a terminal growth rate and compute the terminal value via the
   perpetuity growth method.
5. Discount the explicit-period FCFs and the terminal value back to present value
   and sum them to get enterprise value, then derive fair value per share.
6. Identify the assumptions the result is most sensitive to (e.g., WACC, terminal
   growth rate, near-term margin trajectory).

Your response MUST end with this exact JSON block and nothing after it — this is
required output, not optional:
{{"ticker": "...", "estimated_fair_value_per_share": 0.0, "wacc": 0.0,
  "terminal_growth_rate": 0.0, "diluted_shares_outstanding": 0.0,
  "share_count_source": "filing",
  "most_sensitive_assumptions": ["...", "...", "..."]}}
"""


def run_dcf(ticker: str, company_name: str, filing_context: str, n_years: int = 5) -> dict:
    """
    filing_context: extracted text from the company's 10-Q/10-K —
    do NOT call this with an empty filing_context, the model will
    fabricate numbers (see requirements doc warning on this).
    """
    prompt = DCF_PROMPT_TEMPLATE.format(
        company_name=company_name, ticker=ticker, n_years=n_years
    ) + f"\n\nFiling context:\n{filing_context}"

    raw_text, structured = call_and_extract_json(client, "claude-sonnet-5", prompt, max_tokens=4000)
    return {"raw_output": raw_text, "structured": structured}
