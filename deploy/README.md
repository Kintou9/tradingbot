# Unattended PAPER deployment

The current Electron launcher starts a backend on this Mac and kills that
backend when Electron quits. No always-on host or external monitor was found
in the project configuration or relevant local launch services. These files
prepare a deployment; they do not provision a host, start trading, or verify
SMS delivery. A cloud PostgreSQL database alone does not run the bot.
The current database URL points to this Mac. Azure CLI is installed but has
no signed-in subscriptions, so existing Azure resources could not be checked.

## Starting policy

Keep `ALPACA_PAPER=true` and `LIVE_TRADING_ENABLED=false`. Configure
`MAX_TRADE_NOTIONAL=100`, `MAX_TRADES_PER_DAY=3`,
`MAX_TOTAL_EXPOSURE_PCT=0.30`, `MAX_POSITION_PCT=0.10`, and
`DAILY_LOSS_LIMIT_PCT=0.02`. These limits do not increase automatically.
Entry limits apply across manual and both strategy runners, including open
buy orders and external holdings. Sells that reduce tracked longs are exempt.
The daily entry budget counts submission attempts conservatively, including
cancellations; its day boundary is midnight America/New_York.

New entries are whole-share limit brackets with a persistent GTC broker stop
and target. Orders too small to buy a whole share are skipped; a $100 cap
excludes stocks priced above $100. A valid stop and target are mandatory for
manual entries too (the API can use fresh technical research). Averaging in
is disabled. Bracket legs activate only after the complete entry fill:
partial fills trigger cancellation and a standalone stop on a later protection
tick after cancellation is confirmed. This interval cannot be made atomic.

Existing tracked whole-share positions receive a GTC stop if possible.
Existing fractional positions get DAY stops while the service is running;
they expire and must be renewed. The service flags that limitation and blocks
new entries while such a protection issue exists. Missing stop prices,
broker/database quantity mismatches, and ambiguous orders likewise block
entries and need investigation. Do not blindly overwrite position records.
Protective orders do not guarantee exit prices and do not provide extended
hours protection. Rejected, missing, or externally canceled protection is
checked on the next supervisor tick (normally 30 seconds).

## Deploy to an always-on Linux host

1. Provision a Linux host and a dedicated `tradingbot` service user. Copy this
   checkout to `/opt/trading-bot`; create its Python 3.12 virtual environment
   and install `requirements.txt`. Use PostgreSQL and back it up before rollout.
2. Copy `.env.example` to `.env` on the host, set paper broker credentials,
   database URL, API token, research provider credentials and Twilio settings.
   Restrict `.env` to the service owner. Never copy credentials into service
   unit files or source control. The first startup adds new tables; it does
   not rewrite historical Trade/Position rows.
3. Register a separate dead-man heartbeat monitor (for example Healthchecks
   or an equivalent service). Configure alerts for missing pings after two
   minutes plus a suitable short grace period. Set its HTTPS ping URL in
   `HEARTBEAT_URL`. Verify its alert destination yourself. Failed protection
   cycles do not send success pings. Closing Electron or losing this host
   must produce a monitor alert.
4. Install `trading-bot.service` into `/etc/systemd/system/`, reload systemd,
   and start/enable it after reviewing the configuration. Do not run a second
   backend against a different copy of the database for the same account.
   All supported API/runner entry paths serialize through PostgreSQL advisory
   locks. Session-pooling proxies that break session locks are unsupported.
5. The service listens on loopback. To use the existing local Electron
   dashboard, forward the port with SSH (`ssh -N -L 8000:127.0.0.1:8000 HOST`)
   and use the same dashboard token locally. Stop the local backend first.
   Do not expose port 8000 directly to the internet. `/health` is liveness;
   `/health/ready` is aggregate readiness (503 until healthy). Authenticated
   `/status` includes detailed issues and configured limits.
6. Keep autonomous entries OFF until current positions reconcile and their
   protection is confirmed. Then enable the desired paper strategy in the
   dashboard. Turning entries off or pressing Pause Entries preserves the
   supervisor and exits. It also cancels pending automatic entries on the
   next tick. Protective orders stay at the broker if the process shuts down.

Automatic entries also require SMS configuration, an external heartbeat URL,
and a healthy completed supervisor cycle. The service can still protect
existing positions while those monitoring prerequisites are being set up.
Research runs independently of trading (`RESEARCH_SCHEDULE_ENABLED=true`).
Every five minutes the worker selects up to two active Watcher/trading-list
tickers with missing or 24-hour-old reports. Provider failures retry after
six hours; one failure does not stop other tickers. Expired discovery entries
leave the queue unless they are also on the fixed trading list. The Watcher
shows partial failures and report dates. First or changed Watcher assessments
are queued for SMS. Research uses existing provider credentials and can incur
their normal API charges.

The service reports confirmed fills through a durable SMS outbox, safety
issues at most every 15 minutes after successful send, and an account and
holdings summary after each market close (including early closes). SMS API
acceptance is not proof of handset delivery. Outbox delivery is at least once:
a crash after sending but before committing can duplicate a notification.

## Paper acceptance checks before relying on it

Run `venv/bin/python -m pytest tests`. Automated tests use a fake broker and a
separate SQLite database; they do not place paper or live orders. Then conduct
broker-connected paper checks with the intended host and account:

- Enable paper entries and verify the actual broker bracket/stop orders.
- Restart during a pending order; confirm the same client ID is reconciled,
  with no duplicate position or invented fill.
- Exercise partial fills, cancellation delays/rejections, stale quotes,
  stale research and broker outages. Entry caps must hold and exits continue.
- Pause entries and trigger the daily-loss limit. Confirm pending entries
  cancel and existing protective orders remain.
- Stop the service and verify the independent monitor alerts your phone.
  Restart and verify recovery and the daily digest.

Unknown submissions are deliberately NOT auto-resubmitted or auto-cleared.
Inspect `execution_orders` and look up `client_order_id` in Alpaca. A 404 after
a timeout alone is insufficient evidence that a submission never happened.
Investigate with broker records before a reviewed database repair. Historic
records created by the old estimated-fill path are not silently corrected.
A `broker_binding` row prevents switching the database to another account or
mode; use a separate database for any eventual live rollout.

Paper testing validates operations, not profitability or live fill quality.
Live activation requires a separate review, live credentials, an exact
`LIVE_ACCOUNT_ID`, `LIVE_TRADING_ENABLED=true`, and `ALPACA_PAPER=false`.
The experimental swing runner rejects live execution regardless of that gate.
