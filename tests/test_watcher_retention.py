from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from db.models import Base, WatchedTicker, Position, ResearchNote, DiscoveryRunLog
from db.watcher_retention import expire_watched, expires_at


def test_first_batch_expires_at_two_weeks_while_newer_and_manual_stay():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 18, 20, tzinfo=timezone.utc)
    old = now.replace(tzinfo=None) - timedelta(days=14)
    with sessionmaker(bind=engine)() as db:
        for i in range(10):
            db.add(WatchedTicker(ticker=f'OLD{i}', notes='Auto-discovered 2026-09-04 — tech', added_at=old))
            db.add(WatchedTicker(ticker=f'NEW{i}', notes='Auto-discovered 2026-09-11 — tech', added_at=old+timedelta(days=7)))
        db.add(WatchedTicker(ticker='MANUAL', notes=None, added_at=old-timedelta(days=30)))
        db.add(Position(ticker='OLD0', quantity=1, avg_entry_price=10))
        db.add(ResearchNote(ticker='OLD0', module='technical_scan', raw_output='saved research'))
        db.commit()
        assert expire_watched(db, now-timedelta(microseconds=1)) == []
        assert expire_watched(db, now) == [f'OLD{i}' for i in range(10)]
        assert db.query(WatchedTicker).count() == 11
        assert db.query(Position).one().ticker == 'OLD0'
        assert db.query(ResearchNote).one().raw_output == 'saved research'
        assert expire_watched(db, now) == []
    engine.dispose()


def test_restart_catches_up_all_expired_batches_and_preserves_manual_notes():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    now = datetime(2026, 9, 18)
    with sessionmaker(bind=engine)() as db:
        for i in range(25):
            db.add(WatchedTicker(ticker=f'OLD{i}', notes='Auto-discovered 2026-08-01 — tech', added_at=now-timedelta(days=20)))
        manual = WatchedTicker(ticker='MANUAL', notes='My own pick', added_at=now-timedelta(days=20))
        db.add(manual)
        db.commit()
        assert len(expire_watched(db, now)) == 25
        assert db.query(WatchedTicker).one().ticker == 'MANUAL'
        assert expires_at(manual) is None
    engine.dispose()


def test_expiration_date_matches_stored_addition_time():
    added = datetime(2026, 9, 4, 22, 15)
    stock = WatchedTicker(ticker='TEST', notes='Auto-discovered 2026-09-04 — tech', added_at=added)
    assert expires_at(stock) == datetime(2026, 9, 18, 22, 15)


def test_discovery_does_not_immediately_readd_expired_batch(monkeypatch):
    from engine.discovery import stock_discovery as discovery
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    monkeypatch.setattr(discovery, 'get_positions', lambda: [])
    with sessionmaker(bind=engine)() as db:
        for tickers in ['EARLIER', 'OLD0, OLD1', 'NEW0, NEW1']:
            db.add(DiscoveryRunLog(tickers=tickers))
        db.commit()
        excluded = discovery._already_tracked(db)
        assert {'OLD0', 'OLD1', 'NEW0', 'NEW1'} <= excluded
        assert 'EARLIER' not in excluded
    engine.dispose()
