# Trading Bot

Personal automated trading bot — see `trading_bot_requirements.md` (from our
earlier conversation) for the full spec this scaffold implements.

## Setup

```bash
# 1. Activate your venv (should already exist from terminal setup)
source venv/bin/activate

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set up environment variables
cp .env.example .env
# then fill in .env with your real keys — never commit this file

# 4. Initialize the database (once DATABASE_URL is set in .env)
python -c "from db.session import init_db; init_db()"

# 5. Run the API locally
uvicorn api.main:app --reload
```

Visit `http://127.0.0.1:8000/docs` for the interactive API docs once running.

## Project structure

```
trading-bot/
├── api/                        # FastAPI app — dashboard endpoints, kill switch
│   └── main.py
├── engine/
│   ├── valuation_engine/       # DCF, earnings quality, ticker deep-dive
│   │   ├── dcf.py
│   │   └── deep_dive.py
│   ├── price_action_engine/    # Technical indicators + scanner
│   │   ├── indicators.py
│   │   └── technical_scanner.py
│   ├── news_sentiment/         # News fetching + LLM sentiment scoring
│   │   └── sentiment.py
│   └── entry_exit/             # Combines all signals, gated by risk rules
│       └── signals.py
├── backtesting/
│   └── backtest_runner.py      # vectorbt-based backtesting + walk-forward
├── broker/
│   ├── alpaca_client.py        # Recommended default broker
│   └── robinhood_client.py     # Unofficial, via robin_stocks
├── functions/                  # Azure Functions logic (wire up triggers when deploying)
│   ├── after_hours_research.py
│   └── market_hours_trading.py
├── db/
│   ├── models.py                # Trade, Position, ResearchNote, NewsItem
│   └── session.py
└── dashboard/                   # React app (not yet scaffolded)
```

## Before this trades real money

- [ ] Fill in all `TODO`s — this scaffold is structure, not finished logic
- [ ] Define concrete numeric thresholds for your screening criteria (Section 1 of the requirements doc)
- [ ] Wire up real data injection into the LLM modules — none of them should run on empty context
- [ ] Implement and test risk management rules (max position size, stop-loss, daily loss limit, kill switch)
- [ ] Run backtesting with walk-forward validation before paper trading
- [ ] Run in paper trading mode (`ALPACA_PAPER=true` or Robinhood on a small test amount) before going live
- [ ] Move secrets to Azure Key Vault before any real deployment
