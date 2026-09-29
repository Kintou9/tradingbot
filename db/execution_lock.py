"""Account-wide serialization across API workers and scheduled jobs.

Postgres session advisory locks survive ORM commits and are released when the
connection closes. SQLite uses a file lock (local development/tests only).
"""
from contextlib import contextmanager
import fcntl
import threading

from sqlalchemy import text

_local = threading.local()
_mutexes = {731908412: threading.RLock(), 731908413: threading.RLock()}
LOCK_ID = 731908412


class ExecutionBusy(RuntimeError):
    pass


@contextmanager
def execution_lock(db, lock_id=LOCK_ID):
    depths = getattr(_local, "depths", {})
    _local.depths = depths
    if depths.get(lock_id, 0):
        depths[lock_id] += 1
        try:
            yield
        finally:
            depths[lock_id] -= 1
        return
    mutex = _mutexes[lock_id]
    if not mutex.acquire(blocking=False):
        raise ExecutionBusy("Another trading operation is in progress")
    connection = handle = None
    locked = False
    try:
        engine = db.get_bind()
        if engine.dialect.name == "postgresql":
            connection = engine.connect()
            if not connection.execute(text("SELECT pg_try_advisory_lock(:key)"), {"key": lock_id}).scalar():
                raise ExecutionBusy("Another worker owns the execution lock")
            locked = True
        elif engine.dialect.name == "sqlite":
            database = engine.url.database
            if database and database != ":memory:":
                handle = open(database + f".{lock_id}.execution.lock", "a")
                try:
                    fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError as exc:
                    raise ExecutionBusy("Another worker owns the execution lock") from exc
        else:
            raise RuntimeError("Execution requires PostgreSQL or local SQLite")
        depths[lock_id] = 1
        yield
    finally:
        depths[lock_id] = 0
        try:
            if connection is not None:
                try:
                    if locked:
                        connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": lock_id})
                        connection.commit()
                except Exception:
                    # Never return a possibly locked session to the pool.
                    connection.invalidate()
                    raise
                finally:
                    connection.close()
        finally:
            if handle is not None:
                handle.close()
            mutex.release()
