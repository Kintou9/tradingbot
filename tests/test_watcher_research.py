from datetime import timedelta
import json

import pandas as pd
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, WatchedTicker, ResearchNote, ResearchStatus, NotificationOutbox
from db.research_status import research_state, utcnow
from functions import after_hours_research as research
from functions import scheduled_research as scheduler


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False)
    monkeypatch.setattr(research, 'SessionLocal', factory)
    monkeypatch.setattr(scheduler, 'SessionLocal', factory)
    monkeypatch.setattr(research, 'WATCHLIST', ['CORE'])
    monkeypatch.setenv('RESEARCH_SCHEDULE_ENABLED', 'true')
    monkeypatch.setattr(research, 'fetch_ohlcv', lambda ticker: pd.DataFrame({'close': [10.0]}))
    monkeypatch.setattr(research, 'fetch_news', lambda *a, **kw: [])
    monkeypatch.setattr(research, 'get_company_name', lambda ticker: ticker)
    monkeypatch.setattr(research, 'get_market_data', lambda ticker: {'price': 10})
    monkeypatch.setattr(research, 'get_insider_transactions', lambda ticker: {})
    monkeypatch.setattr(research, 'get_recent_13f_filers', lambda *a: {})
    monkeypatch.setattr(research, 'get_financial_summary', lambda ticker: {})
    monkeypatch.setattr(research, 'run_technical_scan', lambda *a, **kw: {
        'raw_output': 'Technical report', 'structured': {'trend': 'up', 'probability_estimate': 60}})
    monkeypatch.setattr(research, 'run_deep_dive', lambda *a, **kw: {
        'raw_output': 'Company report', 'structured': {'verdict': 'hold', 'confidence': 65}})
    monkeypatch.setattr(research, 'run_dcf', lambda *a, **kw: {
        'raw_output': 'Valuation report', 'structured': {'estimated_fair_value_per_share': 15}})
    import requests
    def no_network(*a, **kw):
        pytest.fail('Unexpected live provider call')
    monkeypatch.setattr(requests.sessions.Session, 'request', no_network)
    yield factory
    engine.dispose()


def test_targets_include_watcher_and_core_but_not_expired_discovery(setup):
    with setup() as db:
        db.add_all([WatchedTicker(ticker='MANUAL'), WatchedTicker(ticker='CORE'),
            WatchedTicker(ticker='DISCOVERED', notes='Auto-discovered today'),
            WatchedTicker(ticker='EXPIRED', notes='Auto-discovered old', added_at=utcnow()-timedelta(days=15))])
        db.commit()
        targets = research.research_targets(db)
        assert set(targets) == {'CORE', 'MANUAL', 'DISCOVERED'}
        assert targets.count('CORE') == 1


def test_scheduler_researches_watcher_without_autonomy_or_market_clock(setup, monkeypatch):
    from db import autonomous_mode, swing_autonomous_mode
    monkeypatch.setattr(autonomous_mode, 'is_autonomous_enabled', lambda: False)
    monkeypatch.setattr(swing_autonomous_mode, 'is_swing_autonomous_enabled', lambda: False)
    with setup() as db:
        db.add_all([WatchedTicker(ticker='FIRST'), WatchedTicker(ticker='SECOND')])
        db.commit()
    first = scheduler.run_scheduled_research()
    assert [item['ticker'] for item in first] == ['FIRST', 'SECOND']
    with setup() as db:
        assert research_state(db, 'FIRST')['status'] == 'current'
        assert db.query(ResearchNote).count() == 6
        assert db.query(NotificationOutbox).count() == 2
    assert [item['ticker'] for item in scheduler.run_scheduled_research()] == ['CORE']
    assert scheduler.run_scheduled_research() == []


def test_failed_technical_module_does_not_block_assessment_or_other_tickers(setup, monkeypatch):
    def fail(*args, **kwargs):
        raise RuntimeError('provider URL with secret must not be persisted')
    monkeypatch.setattr(research, 'run_technical_scan', fail)
    with setup() as db:
        db.add(WatchedTicker(ticker='WATCHED'))
        db.commit()
    results = research.run_after_hours_research()
    assert len(results) == 2
    assert all(item['status'] == 'partial' for item in results)
    with setup() as db:
        state = research_state(db, 'WATCHED')
        assert state['status'] == 'partial' and not state['due']
        assert state['retry_at'] is not None
        assert state['errors'] == ['Technical analysis: RuntimeError']
        assert db.query(ResearchNote).filter(ResearchNote.ticker == 'CORE').count() == 2
        assert research_state(db, 'WATCHED', utcnow()+timedelta(hours=6, seconds=1))['due']


def test_first_and_changed_assessments_notify_but_unchanged_does_not(setup, monkeypatch):
    with setup() as db:
        db.add(WatchedTicker(ticker='WATCHED'))
        db.commit()
        research.research_ticker(db, 'WATCHED')
        research.research_ticker(db, 'WATCHED')
        assert db.query(NotificationOutbox).count() == 1
        monkeypatch.setattr(research, 'run_deep_dive', lambda *a, **kw: {
            'raw_output': 'Updated company report', 'structured': {'verdict': 'avoid'}})
        research.research_ticker(db, 'WATCHED')
        assert db.query(NotificationOutbox).count() == 2
        assert 'AVOID' in db.query(NotificationOutbox).order_by(NotificationOutbox.id.desc()).first().body


def test_stale_assessment_is_marked_and_requeued(setup):
    with setup() as db:
        research.research_ticker(db, 'CORE')
        for note in db.query(ResearchNote):
            note.created_at = utcnow()-timedelta(hours=25)
        db.commit()
        state = research_state(db, 'CORE')
        assert state['status'] == 'stale' and state['assessment_stale'] and state['due']
    assert len(scheduler.run_scheduled_research()) == 1


def test_disabled_research_is_visible_and_makes_no_calls(setup, monkeypatch):
    monkeypatch.setenv('RESEARCH_SCHEDULE_ENABLED', 'false')
    assert scheduler.run_scheduled_research() == []
    with setup() as db:
        assert research_state(db, 'CORE')['status'] == 'paused'
        assert db.query(ResearchNote).count() == 0


def test_incomplete_attempt_retries_after_restart_timeout(setup):
    with setup() as db:
        db.add(ResearchStatus(ticker='CORE', status='running', started_at=utcnow()))
        db.commit()
        assert not research_state(db, 'CORE')['due']
        assert research_state(db, 'CORE', utcnow()+timedelta(hours=1, seconds=1))['due']


def test_failed_assessment_does_not_announce_cached_verdict_as_new(setup, monkeypatch):
    with setup() as db:
        db.add(WatchedTicker(ticker='WATCHED'))
        db.commit()
        research.research_ticker(db, 'WATCHED')
        def fail(*a, **kw):
            raise RuntimeError('unavailable')
        monkeypatch.setattr(research, 'run_deep_dive', fail)
        research.research_ticker(db, 'WATCHED')
        assert db.query(NotificationOutbox).count() == 1
        assert research_state(db, 'WATCHED')['status'] == 'partial'
