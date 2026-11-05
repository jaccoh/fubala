"""The engine runs SQLite in WAL mode with a 30 s busy timeout.

Incident 26 Oct 2026 (02:52Z and 07:00Z): the live DB ran the rollback
journal (journal_mode=delete) with the driver's 5 s default timeout. With
six headless streams writing story_events next to the scheduler and the
HTTP readers, a write that waited more than 5 s raised "database is locked"
and the scheduler loop stopped picking up stories for hours. WAL lets
readers and the writer proceed side by side; the busy timeout makes a
second writer wait instead of failing.
"""

import threading
import time

from sqlalchemy import text

from app.config import Settings
from app.db import SQLITE_BUSY_TIMEOUT_S, make_engine


def test_every_connection_runs_wal_with_the_busy_timeout(tmp_path):
    engine = make_engine(Settings(database_path=tmp_path / "wal.db"))
    with engine.connect() as conn:
        assert conn.execute(text("PRAGMA journal_mode")).scalar() == "wal"
        assert conn.execute(text("PRAGMA busy_timeout")).scalar() == SQLITE_BUSY_TIMEOUT_S * 1000
        # NORMAL (1) is the WAL-safe setting: durable at checkpoints, no fsync per commit
        assert conn.execute(text("PRAGMA synchronous")).scalar() == 1


def test_a_reader_is_not_blocked_by_an_open_write(tmp_path):
    engine = make_engine(Settings(database_path=tmp_path / "rw.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (v INTEGER)"))
        conn.execute(text("INSERT INTO t VALUES (1)"))
    writer = engine.raw_connection()
    try:
        cur = writer.cursor()
        cur.execute("BEGIN IMMEDIATE")
        cur.execute("INSERT INTO t VALUES (2)")
        # the write transaction is open: a reader still sees the committed row at once
        started = time.monotonic()
        with engine.connect() as reader:
            assert reader.execute(text("SELECT count(*) FROM t")).scalar() == 1
        assert time.monotonic() - started < 1.0
        writer.commit()
    finally:
        writer.close()


def test_a_second_writer_waits_for_the_first_instead_of_failing(tmp_path):
    engine = make_engine(Settings(database_path=tmp_path / "ww.db"))
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE t (v INTEGER)"))
    first = engine.raw_connection()
    cur = first.cursor()
    cur.execute("BEGIN IMMEDIATE")
    cur.execute("INSERT INTO t VALUES (1)")
    errors: list[Exception] = []

    def second_writer() -> None:
        try:
            with engine.begin() as conn:
                conn.execute(text("INSERT INTO t VALUES (2)"))
        except Exception as exc:  # noqa: BLE001 - the assertion below names it
            errors.append(exc)

    thread = threading.Thread(target=second_writer)
    thread.start()
    time.sleep(0.3)        # the second writer is now waiting on the lock
    first.commit()
    first.close()
    thread.join(timeout=10)
    assert not thread.is_alive()
    assert errors == []
    with engine.connect() as conn:
        assert conn.execute(text("SELECT count(*) FROM t")).scalar() == 2
