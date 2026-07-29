# Trading Bot — Requirements Doc (Working Draft)

*Starting capital: $20 (paper trading strongly recommended before going live)*

---

## 1. Screening & Universe Definition

- [ ] Define **numeric thresholds**, not vague labels — replace "medium level" and "stocks with debt" with actual ranges:
  - Debt-to-equity ratio range, or interest coverage ratio cutoff
  - Market cap tier (e.g., mid-cap ≈ $2B–$10B) and/or beta range for volatility
  - "Upcoming new stocks" — define precisely: recent IPOs (last N days)? Pre-IPO filings?
- [ ] **Sector/universe whitelist** — limit scanning to sectors you understand deeply (circle of competence), e.g., Technology
- [ ] A defined starting ticker universe (all US equities? S&P 1500? sector-limited list?)

## 2. Valuation Layer ("Margin of Safety")

- [ ] Fundamentals data source (earnings, FCF, book value, growth estimates)
- [ ] Valuation methodology — pick one to start:
  - DCF (most rigorous, needs growth/discount rate assumptions)
  - Comparative valuation (P/E, P/B, PEG vs. historical average or sector peers) — simpler to implement first
  - Graham Number as a quick sanity floor
- [ ] Configurable "discount to intrinsic value" threshold to flag buy candidates (e.g., only flag if price is 20%+ below calculated value)
- [ ] Build as a separate `valuation_engine` module

## 3. Price Action / Technical Signal Layer

- [ ] OHLCV historical data feed (daily bars to start; intraday later if needed)
- [ ] Configurable, testable technical indicators: moving average crossovers, RSI, volume spikes, support/resistance
- [ ] Build as a separate `price_action_engine` module

## 4. Entry / Exit Signal Engine

- [ ] **Entry logic**: combine valuation + price action + sentiment (e.g., price below intrinsic value AND neutral/positive sentiment)
- [ ] **Exit logic** — treat as a distinct problem from entry:
  - Value-based exit (price re-rates above intrinsic value)
  - Stop-loss / trailing stop (risk-based)
  - Time-based re-evaluation after N days
- [ ] Log the *reason* for every trade (valuation trigger, stop-loss, manual) in the trade history table

## 5. Backtesting

- [ ] Use an existing framework (`backtrader`, `vectorbt`, or Alpaca's tools) — don't build from scratch
- [ ] Multi-year historical data store, separate from the live data feed
- [ ] **Walk-forward testing**: tune on one period, validate on a later unseen period
- [ ] Performance metrics: Sharpe ratio, max drawdown, win rate, avg win vs. avg loss — not just total return
- [ ] Design around known pitfalls:
  - **Survivorship bias** — use point-in-time historical universes, not today's surviving-company list
  - **Look-ahead bias** — data must reflect only what was actually available at that point in time
  - **Overfitting** — a backtest tuned to perfection on $20/narrow universe is a red flag, not a win
- [ ] Run strategy through backtest → paper trading → live, in that order

## 6. Data Sources

**Price / OHLCV / technical indicators:**
- Alpha Vantage — deep historical coverage + indicators, but free tier is thin (25 requests/day)
- Twelve Data — more workable free tier (800 calls/day), ~4hr delay
- Finnhub — higher throughput free tier (60 calls/min), ~20min delay
- Polygon.io ("Massive") — best for real-time/WebSocket on paid plans; free tier only ~1yr history
- Avoid Yahoo Finance as a core dependency — reliability has degraded

**Fundamentals:** Alpha Vantage or Twelve Data (both bundle fundamentals with price data)

**News/sentiment:** Finnhub or Benzinga structured news APIs, scored via LLM (Claude API) rather than keyword matching

> Architecture note: pull numeric OHLCV data → store in PostgreSQL → compute indicators/backtests from numbers → render your own charts for the dashboard. Don't have the bot visually "read" chart images — numeric analysis is far more reliable than image-based pattern recognition.

## 7. Risk Management

- [ ] Max position size per trade (% of capital)
- [ ] Stop-loss and take-profit thresholds
- [ ] Max daily/weekly loss limit that halts the bot automatically
- [ ] Manual kill switch, accessible from your phone

## 8. Infrastructure & Scheduling

- [ ] Broker/execution API — Alpaca is the common choice (paper trading, fractional shares, clean REST API)
- [ ] Market-hours awareness, including holidays and early-close days
- [ ] Azure Functions with timer triggers:
  - One timer for after-hours research (news + valuation scans)
  - One for market-hours trading logic
- [ ] Default to paper trading mode; explicit flag required to go live
- [ ] PostgreSQL: trade history, P&L, ingested news items
- [ ] React dashboard to track account growth over time
- [ ] API keys in Azure Key Vault, never in code

## 9. Notifications

- [ ] Twilio SMS integration
- [ ] Define triggers: every trade? Daily summary? Only losses over X%?
- [ ] Separate alert channel for bot failures/crashes, not just trading news

## 10. Compliance / Constraints to Design Around

- [ ] **PDT rule**: $25k minimum equity needed for 4+ day trades in 5 business days in a margin account — with $20, plan around this (cash account + T+1 settlement, or low trade frequency)
- [ ] Fractional share support required at this capital level (Alpaca, Public support this; many brokers don't)
- [ ] Check broker ToS — some prohibit automated bots
- [ ] Wash sale rule awareness for tax reporting if trading the same ticker frequently

## 11. LLM-Based Analyst Sub-modules

Two additional prompt-driven sub-modules to run inside the after-hours research job, alongside the earnings-quality and DCF templates already in the pipeline. Both feed the entry/exit engine (Section 4) rather than replacing it — the risk management rules in Section 7 always take final say over position sizing and stop-losses, regardless of what these modules output.

### 11a. Ticker Deep Dive & Red Flags
Fundamental snapshot module — runs per ticker in the watchlist, on a schedule (e.g., after each earnings release, or weekly).

Covers: price/market cap/52-week range, one-line business description + moat score, trailing 4-quarter revenue/EPS growth + surprise history, valuation multiples vs. sector and 5-year average, top bullish catalysts, top bearish red flags, insider/institutional activity (last 6 months), and a verdict + confidence score.

**Data requirements** (same principle as the earnings-quality/DCF modules — real data in, not model recall):
- Price/valuation multiples: your existing OHLCV + fundamentals API (Alpha Vantage / Twelve Data)
- Insider activity: SEC EDGAR Form 4 filings, or Finnhub's insider transactions endpoint
- Institutional activity: 13F filings via SEC EDGAR full-text search (note: 13Fs are filed quarterly with a lag, so "last 6 months" will really mean the two most recent filed quarters)
- The "verdict + confidence" output is an internal bot signal, not a recommendation to act on directly — it's one input the entry/exit engine weighs alongside valuation and price action, gated by your risk rules

### 11b. Technical Setup & Entry/Exit Scanner
Price action module — the concrete implementation of the `price_action_engine` from Section 3.

Covers: trend direction + 50/200DMA, support/resistance (horizontal + Fibonacci), volume profile/spikes, RSI/MACD/Bollinger signals, proposed entry range + stop-loss, two price targets + reward:risk ratio, and a probability-of-success estimate.

**Data requirements:**
- All indicators (RSI, MACD, Bollinger, moving averages) computed programmatically from real OHLCV bars — don't have the LLM estimate these from a text description, calculate them directly (Alpha Vantage/Twelve Data both expose these as indicator endpoints, or compute in-code with `pandas`/`ta-lib`)
- Support/resistance and Fibonacci levels can be either rule-based (swing high/low detection) or LLM-assisted once fed the real price series
- Chart visualization: render with matplotlib/Plotly for the dashboard rather than having the LLM "describe" a chart

### Output format for both
Have each module return a structured JSON block (verdict, confidence, entry/stop/target prices, key scores) alongside the human-readable write-up, so your code can parse and log results directly into PostgreSQL rather than scraping markdown — matches the same pattern noted for the earnings-quality/DCF prompts in Section 6.

---

## 12. Tech Stack (Confirmed: Python)

| Layer | Choice |
|---|---|
| Bot core / API | Python + FastAPI |
| Data/analysis | `pandas`, `numpy`, `ta-lib` or `pandas-ta` |
| Backtesting | `vectorbt` or `backtrader` |
| Database | Azure PostgreSQL (Flexible Server) |
| Scheduling | Azure Functions (Timer trigger) — after-hours research job + market-hours trading loop |
| Broker execution | `alpaca-py` and/or `robin_stocks` |
| LLM calls (sentiment, deep-dive, DCF) | Claude API via `anthropic` Python SDK |
| SMS notifications | Twilio Python SDK |
| Secrets | Azure Key Vault |
| Dashboard | React (consumes FastAPI endpoints) |

### Project structure
```
trading-bot/
├── api/                  # FastAPI app — dashboard endpoints, manual kill switch
├── engine/
│   ├── valuation_engine/
│   ├── price_action_engine/
│   ├── news_sentiment/
│   └── entry_exit/
├── backtesting/
├── broker/               # alpaca-py or robin_stocks wrapper
├── functions/            # Azure Functions: after-hours job, market-hours loop
├── db/                   # PostgreSQL models/migrations
└── dashboard/            # React app
```

---

## Open Items / Still Deciding
*(add here as you gather more input)*

-
