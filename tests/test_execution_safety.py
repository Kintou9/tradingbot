"""Failure-oriented execution tests. No broker, database, SMS or web access."""
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace as NS
import os
import threading

# Isolate imported SDK/DB initialization from developer credentials. dotenv
# respects these preexisting values. No startup handlers run in these tests.
os.environ["ALPACA_API_KEY"] = "test-key"
os.environ["ALPACA_SECRET_KEY"] = "test-secret"
os.environ["DATABASE_URL"] = "sqlite:///:memory:"
os.environ["ALPACA_PAPER"] = "true"

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, ExecutionOrder, Position, Trade, SwingPosition, SwingTrade, NotificationOutbox
from db.execution_lock import execution_lock, ExecutionBusy
from engine.entry_exit import execution as ex
from engine.entry_exit.risk import RiskRejected, Limits, day_start_utc


class FakeBroker:
    def __init__(self):
        self.orders = {}
        self.holdings = {}
        self.submissions = []
        self.account = NS(id="test-account", equity="10000", last_equity="10000", cash="10000",
                          buying_power="10000", trading_blocked=False, account_blocked=False)
        self.open = True
        self.ask = 20.0
        self.submit_timeout = False
        self.cancel_pending = False
        self.fill_on_cancel = False

    def get_account(self):
        return self.account

    def get_all_positions(self):
        return list(self.holdings.values())

    def get_clock(self):
        return NS(is_open=self.open)

    def get_orders(self, request):
        result = list(self.orders.values())
        if request.status == "open":
            result = [o for o in result if o.status not in ex.TERMINAL]
        if request.after:
            result = [o for o in result if o.created_at >= request.after]
        return deepcopy(result)

    def submit_order(self, request):
        self.submissions.append(request)
        order = NS(id=f"order-{len(self.orders)}", client_order_id=request.client_order_id,
            symbol=request.symbol, side=request.side.value, qty=request.qty, filled_qty=0,
            filled_avg_price=None, status="new", legs=[],
            limit_price=getattr(request, "limit_price", None), stop_price=getattr(request, "stop_price", None),
            type=request.type.value, time_in_force=request.time_in_force.value,
            created_at=datetime.now(timezone.utc))
        self.orders[order.id] = order
        if self.submit_timeout:
            raise TimeoutError("accepted but response lost")
        return deepcopy(order)

    def get_order_by_id(self, id, filter=None):
        return deepcopy(self.orders[id])

    def get_order_by_client_id(self, client_id):
        return deepcopy(next(o for o in self.orders.values() if o.client_order_id == client_id))

    def cancel_order_by_id(self, id):
        order = self.orders[id]
        if self.fill_on_cancel:
            self.fill(id, order.qty, 18)
        else:
            order.status = "pending_cancel" if self.cancel_pending else "canceled"
            for leg in order.legs:
                leg.status = order.status
                self.orders[leg.id].status = order.status

    def fill(self, id, qty, price):
        order = self.orders[id]
        old = order.filled_qty
        order.filled_qty, order.filled_avg_price = qty, price
        order.status = "filled" if qty == order.qty else "partially_filled"
        current = self.holdings.get(order.symbol)
        remaining = (float(current.qty) if current else 0) + (qty-old) * (1 if order.side == "buy" else -1)
        if remaining > 0:
            self.holdings[order.symbol] = NS(symbol=order.symbol, qty=remaining, market_value=remaining*price,
                avg_entry_price=price, current_price=price)
        else:
            self.holdings.pop(order.symbol, None)


@pytest.fixture
def env(monkeypatch):
    import requests
    def no_network(*args, **kwargs):
        raise AssertionError("Tests must not make real network requests")
    monkeypatch.setattr(requests.sessions.Session, "request", no_network)
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    db = factory()
    fake = FakeBroker()
    monkeypatch.setattr(ex.broker, "client", fake)
    monkeypatch.setattr(ex.broker, "get_account", fake.get_account)
    monkeypatch.setattr(ex.broker, "get_positions", fake.get_all_positions)
    monkeypatch.setattr(ex.broker, "fresh_ask", lambda ticker: fake.ask)
    monkeypatch.setattr(ex.broker, "PAPER", True)
    monkeypatch.setattr(ex, "is_bot_enabled", lambda: True)
    monkeypatch.setattr(ex, "set_bot_enabled", lambda *a, **kw: None)
    import db.autonomous_mode as autonomous
    import db.swing_autonomous_mode as swing
    monkeypatch.setattr(autonomous, "is_autonomous_enabled", lambda: True)
    monkeypatch.setattr(swing, "is_swing_autonomous_enabled", lambda: True)
    for key, value in {"MAX_TRADE_NOTIONAL": "100", "MAX_TRADES_PER_DAY": "3", "MAX_TOTAL_EXPOSURE_PCT": "0.30",
                       "MAX_POSITION_PCT": "0.10", "DAILY_LOSS_LIMIT_PCT": "0.02", "LIVE_TRADING_ENABLED": "false"}.items():
        monkeypatch.setenv(key, value)
    yield NS(db=db, factory=factory, fake=fake, monkeypatch=monkeypatch)
    db.close()
    engine.dispose()


def buy(env, ticker="TEST", **kw):
    return ex.execute_buy(env.db, ticker, stop_loss_price=18, take_profit_price=25, **kw)


def holding(env, ticker="OLD", qty=2, current=20, stop=18):
    env.db.add(Position(ticker=ticker, quantity=qty, avg_entry_price=20,
                        stop_loss_price=stop, take_profit_price=25))
    env.db.commit()
    env.fake.holdings[ticker] = NS(symbol=ticker, qty=qty, market_value=qty*current,
                                  avg_entry_price=20, current_price=current)


def test_limit_bracket_caps_cost_and_does_not_invent_fill(env):
    result = buy(env)
    request = env.fake.submissions[0]
    assert request.qty * request.limit_price <= 100
    assert request.order_class == "bracket" and request.time_in_force == "gtc"
    assert request.stop_loss.stop_price == 18
    assert result["qty"] == 0 and result["price"] is None
    assert env.db.query(Trade).count() == env.db.query(Position).count() == 0


@pytest.mark.parametrize("qty", [0, -1, float("nan"), float("inf"), 0.5, 10])
def test_invalid_fractional_or_oversize_override_cannot_bypass_cap(env, qty):
    with pytest.raises(RiskRejected):
        buy(env, qty=qty)
    assert not env.fake.submissions


def test_loss_switch_and_paused_entries_do_not_block_exit(env):
    holding(env, current=20)
    env.monkeypatch.setattr(ex, "is_bot_enabled", lambda: False)
    with pytest.raises(RiskRejected):
        buy(env)
    result = ex.execute_sell(env.db, "OLD", 2, "stop_loss")
    assert result["action"] == "sell"
    assert env.fake.submissions[-1].side == "sell"


def test_daily_loss_blocks_manual_buy(env):
    env.fake.account.equity = "9799"
    with pytest.raises(RiskRejected, match="Daily loss"):
        buy(env)
    assert not env.fake.submissions


def test_daily_entry_count_includes_other_strategy_and_external_orders(env):
    buy(env, "ONE")
    buy(env, "TWO", strategy="swing", metadata={"entry_session_date": "2026-09-17"})
    buy(env, "THREE")
    with pytest.raises(RiskRejected, match="Daily entry"):
        buy(env, "FOUR")
    assert len(env.fake.submissions) == 3


def test_pending_buys_reserve_total_exposure(env):
    env.fake.account.equity = env.fake.account.last_equity = "1000"
    env.monkeypatch.setenv("MAX_TOTAL_EXPOSURE_PCT", "0.10")
    buy(env, "ONE")
    with pytest.raises(RiskRejected, match="exposure"):
        buy(env, "TWO")


def test_timeout_reconciles_by_client_id_after_restart_without_resubmit(env):
    env.fake.submit_timeout = True
    with pytest.raises(RuntimeError, match="unknown"):
        buy(env)
    row = env.db.query(ExecutionOrder).one()
    assert row.status == "unknown"
    order = next(iter(env.fake.orders.values()))
    env.fake.fill(order.id, order.qty, 19)
    env.db.close()
    env.db = env.factory()
    assert ex.reconcile_orders(env.db) == []
    assert env.db.query(Trade).one().price == 19
    ex.reconcile_orders(env.db)
    assert env.db.query(Trade).count() == 1
    assert len(env.fake.submissions) == 1


def test_unknown_submission_not_found_stays_reserved(env):
    env.fake.submit_timeout = True
    with pytest.raises(RuntimeError):
        buy(env)
    env.fake.orders.clear()  # broker lookup failure/404 does not prove no order
    with pytest.raises(RiskRejected, match="Reconciliation"):
        buy(env, "SECOND")
    assert env.db.query(ExecutionOrder).one().status == "unknown"
    assert len(env.fake.submissions) == 1


def test_partial_fills_apply_only_increment_and_correct_incremental_price(env):
    result = buy(env)
    env.fake.fill(result["order_id"], 1, 19)
    ex.reconcile_orders(env.db)
    env.fake.fill(result["order_id"], 4, 19.75)
    ex.reconcile_orders(env.db)
    ex.reconcile_orders(env.db)
    trades = env.db.query(Trade).order_by(Trade.id).all()
    assert [(t.quantity, t.price) for t in trades] == [(1, 19), (3, 20)]
    pos = env.db.query(Position).one()
    assert pos.quantity == 4 and pos.avg_entry_price == 19.75
    assert env.db.query(NotificationOutbox).count() == 2


def test_partial_entry_is_canceled_then_filled_quantity_is_protected(env):
    result = buy(env)
    env.fake.fill(result["order_id"], 1, 19)
    issues = ex.maintain_protection(env.db)
    assert not issues
    assert env.fake.orders[result["order_id"]].status == "canceled"
    stop = env.fake.submissions[-1]
    assert stop.side == "sell" and stop.qty == 1 and stop.stop_price == 18
    assert env.db.query(Position).one().quantity == 1


def test_cancellation_delay_does_not_send_duplicate_exit(env):
    holding(env)
    ex.maintain_protection(env.db)
    env.fake.cancel_pending = True
    with pytest.raises(RiskRejected, match="cancellation pending"):
        ex.execute_sell(env.db, "OLD", 2, "manual")
    assert len(env.fake.submissions) == 1  # original stop only


def test_stop_fill_during_cancel_prevents_oversell(env):
    holding(env)
    ex.maintain_protection(env.db)
    env.fake.fill_on_cancel = True
    result = ex.execute_sell(env.db, "OLD", 2, "manual")
    assert result["status"] == "already_closed"
    assert len(env.fake.submissions) == 1
    assert env.db.query(Position).count() == 0
    assert env.db.query(Trade).one().quantity == 2


def test_fractional_legacy_gets_day_stop_and_health_warning(env):
    holding(env, qty=0.5)
    issues = ex.maintain_protection(env.db)
    assert any("fractional" in issue for issue in issues)
    assert env.fake.submissions[0].time_in_force == "day"
    with pytest.raises(RiskRejected, match="need attention"):
        buy(env)


def test_live_flag_alone_cannot_submit(env):
    env.monkeypatch.setattr(ex.broker, "PAPER", False)
    with pytest.raises(ValueError, match="locked"):
        buy(env)
    assert not env.fake.submissions


def test_live_account_id_must_match(env):
    env.monkeypatch.setattr(ex.broker, "PAPER", False)
    env.monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    env.monkeypatch.setenv("LIVE_ACCOUNT_ID", "some-other-account")
    with pytest.raises(ValueError, match="does not match"):
        buy(env)
    assert not env.fake.submissions


def test_account_switch_refuses_old_database(env):
    buy(env)
    env.fake.account.id = "different-account"
    with pytest.raises(RiskRejected, match="different broker"):
        buy(env, "SECOND")


def test_swing_fill_stays_in_separate_tables_and_honors_quantity_cap(env):
    result = buy(env, strategy="swing", qty_cap=2,
        metadata={"entry_session_date": "2026-09-17", "strategy_version": "test"})
    assert env.fake.submissions[0].qty == 2
    env.fake.fill(result["order_id"], 2, 19)
    ex.reconcile_orders(env.db)
    assert env.db.query(Trade).count() == env.db.query(Position).count() == 0
    assert env.db.query(SwingTrade).one().price == 19
    assert env.db.query(SwingPosition).one().quantity == 2


def test_swing_live_execution_refused_even_with_live_gate(env):
    env.monkeypatch.setattr(ex.broker, "PAPER", False)
    env.monkeypatch.setenv("LIVE_TRADING_ENABLED", "true")
    env.monkeypatch.setenv("LIVE_ACCOUNT_ID", "test-account")
    with pytest.raises(RiskRejected, match="paper-only"):
        buy(env, strategy="swing")


def test_rejected_order_is_not_a_trade(env):
    result = buy(env)
    env.fake.orders[result["order_id"]].status = "rejected"
    ex.reconcile_orders(env.db)
    assert env.db.query(Trade).count() == env.db.query(Position).count() == 0


def test_bracket_child_fill_reconciles_after_restart(env):
    result = buy(env)
    parent = env.fake.orders[result["order_id"]]
    env.fake.fill(parent.id, parent.qty, 20)
    child = NS(id="child-stop", client_order_id="child-client", symbol="TEST", side="sell",
        qty=parent.qty, filled_qty=parent.qty, filled_avg_price=18, status="filled", legs=[],
        stop_price=18, limit_price=None, created_at=datetime.now(timezone.utc))
    parent.legs = [child]
    env.fake.orders[child.id] = child
    env.fake.holdings.clear()
    ex.reconcile_orders(env.db)
    ex.reconcile_orders(env.db)
    assert env.db.query(Position).count() == 0
    assert [(t.action, t.quantity) for t in env.db.query(Trade).order_by(Trade.id)] == [("buy", 4), ("sell", 4)]


def test_entry_mutex_excludes_concurrent_worker(env):
    errors = []
    with execution_lock(env.db):
        def attempt():
            try:
                with execution_lock(env.db):
                    errors.append("unsafe concurrent entry")
            except ExecutionBusy:
                errors.append("blocked")
        worker = threading.Thread(target=attempt)
        worker.start()
        worker.join()
    assert errors == ["blocked"]


def test_ny_day_boundary_including_dst():
    assert day_start_utc(datetime(2026, 9, 17, 2, tzinfo=timezone.utc)) == datetime(2026, 9, 16, 4)
    assert day_start_utc(datetime(2026, 1, 17, 2, tzinfo=timezone.utc)) == datetime(2026, 1, 16, 5)


@pytest.mark.parametrize("key,value", [("MAX_TRADE_NOTIONAL", "nan"), ("MAX_POSITION_PCT", "2"),
    ("MAX_TRADES_PER_DAY", "1.5"), ("DAILY_LOSS_LIMIT_PCT", "0")])
def test_bad_configuration_fails_closed(env, key, value):
    env.monkeypatch.setenv(key, value)
    with pytest.raises(RiskRejected):
        Limits.from_env()


def test_paused_autonomy_cancels_pending_automatic_entry(env):
    from notifications import monitoring
    env.monkeypatch.setattr(monitoring, "health_state", lambda db: {"healthy": True})
    result = buy(env, reason="valuation_trigger")
    import db.autonomous_mode as autonomous
    env.monkeypatch.setattr(autonomous, "is_autonomous_enabled", lambda: False)
    ex.maintain_protection(env.db)
    assert env.fake.orders[result["order_id"]].status == "canceled"


def test_stale_entry_is_canceled_not_left_gtc_overnight(env):
    result = buy(env)
    env.db.query(ExecutionOrder).one().created_at = datetime.utcnow() - timedelta(seconds=61)
    env.db.commit()
    ex.maintain_protection(env.db)
    assert env.fake.orders[result["order_id"]].status == "canceled"


def test_account_exposure_includes_untracked_external_holdings(env):
    env.fake.holdings["EXTERNAL"] = NS(symbol="EXTERNAL", qty=10, market_value=2950)
    with pytest.raises(RiskRejected, match="exposure"):
        buy(env)


def test_no_entry_when_quote_unavailable(env):
    def missing(ticker):
        raise ValueError("stale quote")
    env.monkeypatch.setattr(ex.broker, "fresh_ask", missing)
    with pytest.raises(ValueError, match="stale quote"):
        buy(env)
    assert not env.fake.submissions


def test_unrelated_unknown_order_does_not_block_protective_exit(env):
    env.fake.submit_timeout = True
    with pytest.raises(RuntimeError):
        buy(env, "UNKNOWN")
    env.fake.orders.clear()
    env.fake.submit_timeout = False
    holding(env)
    result = ex.execute_sell(env.db, "OLD", 2, "stop_loss")
    assert result["action"] == "sell"


def test_missing_stop_does_not_crash_optional_target_exit(env):
    from engine.entry_exit.signals import evaluate_exit
    assert evaluate_exit("TEST", 20, 30, None, 25).reason == "take_profit"
    assert evaluate_exit("TEST", 20, 20, None, None) is None


def test_stale_research_and_disabled_autonomy_do_not_disable_exits(env):
    from functions import market_hours_trading as loop
    holding(env, ticker="REMOVED_FROM_WATCHLIST", current=26)
    env.monkeypatch.setattr(loop, "get_account", env.fake.get_account)
    env.monkeypatch.setattr(loop, "get_positions", env.fake.get_all_positions)
    env.monkeypatch.setattr(loop, "is_market_hours", lambda: True)
    env.monkeypatch.setattr(loop, "is_bot_enabled", lambda: False)
    env.monkeypatch.setattr(loop, "is_autonomous_enabled", lambda: False)
    env.monkeypatch.setattr(loop, "_latest_research", lambda *a: pytest.fail("Exits must not load research"))
    result = loop._run_trading_cycle(env.db)
    assert result["market_open"]
    assert env.fake.submissions[-1].side == "sell"
    assert getattr(env.fake.submissions[-1], "stop_price", None) is None  # market exit after stop cancellation


def test_daily_loss_cancels_pending_entries_but_keeps_stops(env):
    from functions import market_hours_trading as loop
    result = buy(env)
    holding(env, ticker="HELD")
    env.fake.account.equity = "9700"
    enabled = [True]
    def pause(*args, **kwargs):
        enabled[0] = False
    env.monkeypatch.setattr(loop, "get_account", env.fake.get_account)
    env.monkeypatch.setattr(loop, "get_positions", env.fake.get_all_positions)
    env.monkeypatch.setattr(loop, "is_market_hours", lambda: True)
    env.monkeypatch.setattr(loop, "is_bot_enabled", lambda: enabled[0])
    env.monkeypatch.setattr(loop, "set_bot_enabled", pause)
    env.monkeypatch.setattr(loop, "is_autonomous_enabled", lambda: True)
    env.monkeypatch.setattr(ex, "is_bot_enabled", lambda: enabled[0])
    loop._run_trading_cycle(env.db)
    assert not enabled[0]
    assert env.fake.orders[result["order_id"]].status == "canceled"
    assert any(o.symbol == "HELD" and o.stop_price == 18 and o.status == "new" for o in env.fake.orders.values())


def test_health_detects_dead_worker_and_missing_alert_configuration(env):
    from notifications import monitoring
    from db.models import RuntimeState
    env.monkeypatch.setattr(monitoring, "is_configured", lambda: False)
    env.monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    env.db.add(RuntimeState(key="supervisor", value='{"issues": []}',
                            updated_at=datetime.utcnow()-timedelta(seconds=121)))
    env.db.commit()
    state = monitoring.health_state(env.db)
    assert not state["healthy"]
    assert len(state["issues"]) == 3


def test_unhealthy_cycle_never_sends_success_heartbeat(env):
    from notifications import monitoring
    env.monkeypatch.setenv("HEARTBEAT_URL", "https://monitor.example/ping")
    env.monkeypatch.setattr(monitoring.requests, "get", lambda *a, **kw: pytest.fail("false healthy heartbeat"))
    env.monkeypatch.setattr(monitoring, "send_sms", lambda *a: True)
    monitoring.report_cycle(env.db, ["Broker unavailable"])
    assert not monitoring.health_state(env.db)["healthy"]


def test_notification_outbox_retries_without_losing_confirmed_fill(env):
    from notifications import monitoring
    result = buy(env)
    env.fake.fill(result["order_id"], 4, 19)
    ex.reconcile_orders(env.db)
    env.monkeypatch.delenv("HEARTBEAT_URL", raising=False)
    env.monkeypatch.setattr(monitoring, "send_sms", lambda *a: False)
    monitoring.report_cycle(env.db, [])
    assert env.db.query(NotificationOutbox).one().sent_at is None
    env.monkeypatch.setattr(monitoring, "send_sms", lambda *a: True)
    monitoring.report_cycle(env.db, [])
    assert env.db.query(NotificationOutbox).one().sent_at is not None
    assert env.db.query(Trade).count() == 1


def test_broker_rejection_of_stop_surfaces_health_issue(env):
    holding(env)
    env.fake.submit_timeout = True
    issues = ex.maintain_protection(env.db)
    assert any("protection failed" in i for i in issues)
    assert env.db.query(ExecutionOrder).one().status == "unknown"


def test_fresh_quote_transport_rejects_stale_or_zero_ask(env):
    quote = NS(timestamp=datetime.now(timezone.utc)-timedelta(seconds=61), ask_price=20)
    data = NS(get_stock_latest_quote=lambda request: {"TEST": quote})
    env.monkeypatch.setattr(ex.broker, "StockHistoricalDataClient", lambda *a: data)
    env.monkeypatch.setattr(ex.broker, "_bounded", lambda obj: obj)
    # Use the real quote validation instead of the fixture's fresh-ask override.
    # Original function kept below before fixture replacement via module-level alias.
    with pytest.raises(ValueError, match="stale"):
        REAL_FRESH_ASK("TEST")


REAL_FRESH_ASK = ex.broker.fresh_ask


def test_autonomous_entries_require_working_monitoring(env):
    from notifications import monitoring
    env.monkeypatch.setattr(monitoring, "health_state", lambda db: {"healthy": False})
    with pytest.raises(RiskRejected, match="healthy monitoring"):
        buy(env, reason="valuation_trigger")
    assert not env.fake.submissions


def test_separate_research_lock_does_not_block_protection(env):
    acquired = []
    with execution_lock(env.db, lock_id=731908413):
        def protect():
            with execution_lock(env.db):
                acquired.append(True)
        thread = threading.Thread(target=protect)
        thread.start()
        thread.join()
    assert acquired == [True]


def test_file_lock_excludes_another_process(env, tmp_path):
    import subprocess
    import sys
    path = tmp_path / "execution.db"
    engine = create_engine("sqlite:///" + str(path))
    factory = sessionmaker(bind=engine)
    db = factory()
    script = '''
import sys
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from db.execution_lock import execution_lock, ExecutionBusy
session = sessionmaker(bind=create_engine("sqlite:///" + sys.argv[1]))()
try:
    with execution_lock(session):
        print("unsafe")
except ExecutionBusy:
    print("blocked")
'''
    try:
        with execution_lock(db):
            result = subprocess.run([sys.executable, "-c", script, str(path)], capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert result.stdout.strip() == "blocked"
    finally:
        db.close()
        engine.dispose()


def test_api_enforces_caps_and_validates_quantities(env, tmp_path):
    from fastapi.testclient import TestClient
    from api import main
    api_engine = create_engine("sqlite:///" + str(tmp_path / "api.db"))
    Base.metadata.create_all(api_engine)
    env.monkeypatch.setattr(main, "SessionLocal", sessionmaker(bind=api_engine))
    env.monkeypatch.setattr(main, "DASHBOARD_API_TOKEN", "test-api-token")
    # No context manager: startup workers are intentionally NOT started.
    client = TestClient(main.app)
    headers = {"X-API-Token": "test-api-token"}
    invalid = client.post("/positions/TEST/buy", headers=headers, json={"qty": -1})
    assert invalid.status_code == 422
    oversized = client.post("/positions/TEST/buy", headers=headers,
        json={"qty": 20, "stop_loss_price": 18, "take_profit_price": 25})
    assert oversized.status_code == 409
    assert "cap" in oversized.json()["detail"]
    assert not env.fake.submissions
    env.monkeypatch.setenv("TRADING_MODE", "live")
    assert client.get("/health").json()["trading_mode"] == "paper"
    client.close()
    api_engine.dispose()


def test_account_digest_is_once_per_market_session(env):
    from notifications import monitoring
    from db.models import RuntimeState
    from datetime import time
    env.monkeypatch.setattr(env.fake, "get_calendar", lambda request: [NS(close=datetime.combine(request.start, time.min))], raising=False)
    env.monkeypatch.setattr(monitoring.broker, "client", env.fake)
    sent = []
    env.monkeypatch.setattr(monitoring, "send_sms", lambda body: sent.append(body) or True)
    monitoring.send_account_digest(env.db)
    monitoring.send_account_digest(env.db)
    assert len(sent) == 1
    assert "daily summary" in sent[0]


def test_research_job_does_not_claim_success_after_exception(env):
    from functions import scheduled_research as job
    from db.models import ResearchStatus
    env.monkeypatch.setattr(job, "SessionLocal", env.factory)
    env.monkeypatch.setattr(job, "research_targets", lambda db: ["TEST"])
    env.monkeypatch.setattr(job, "research_state", lambda *args: {"due": True, "last_attempt_at": None})
    env.monkeypatch.setenv("RESEARCH_SCHEDULE_ENABLED", "true")
    def fail(**kwargs):
        raise RuntimeError("database unavailable")
    env.monkeypatch.setattr(job, "run_after_hours_research", fail)
    with pytest.raises(RuntimeError):
        job.run_scheduled_research()
    assert env.db.query(ResearchStatus).count() == 0


def test_weakened_stop_is_not_reported_as_full_protection(env):
    holding(env)
    ex.maintain_protection(env.db)
    stop = next(iter(env.fake.orders.values()))
    stop.stop_price = 1
    issues = ex.maintain_protection(env.db)
    assert any("coverage is unconfirmed" in issue for issue in issues)


def test_pausing_entries_durably_queues_alert(env):
    from db import kill_switch
    env.monkeypatch.setattr(kill_switch, "SessionLocal", env.factory)
    kill_switch.set_bot_enabled(False, reason="daily_loss_limit_hit")
    assert not kill_switch.is_bot_enabled()
    message = env.db.query(NotificationOutbox).one()
    assert "daily_loss_limit_hit" in message.body and message.sent_at is None
