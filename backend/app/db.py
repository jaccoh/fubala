"""Database engine and helpers."""

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.engine import Engine
from sqlmodel import Session, SQLModel, create_engine

from . import models  # noqa: F401  (registers tables)
from .config import Settings

# A writer waits this long for the lock before "database is locked".
SQLITE_BUSY_TIMEOUT_S = 30


def make_engine(settings: Settings) -> Engine:
    path = Path(settings.database_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": SQLITE_BUSY_TIMEOUT_S},
    )
    event.listen(engine, "connect", _sqlite_pragmas)
    return engine


def _sqlite_pragmas(dbapi_connection, _connection_record) -> None:
    """WAL on every connection: readers never wait on the writer and the
    writer never waits on readers. The rollback journal (journal_mode=delete)
    made them block each other, and with the driver's 5 s default a busy
    minute raised "database is locked" and stopped the scheduler loop
    (26 Oct 02:52Z and 07:00Z). WAL persists in the file; /data is local
    ext4 (WAL needs a local filesystem). synchronous=NORMAL is the WAL-safe
    setting: durable at checkpoints, no fsync per commit."""
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute("PRAGMA synchronous=NORMAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_S * 1000}")
    cursor.close()


def init_db(engine: Engine) -> None:
    SQLModel.metadata.create_all(engine)


@contextmanager
def session_scope(engine: Engine) -> Iterator[Session]:
    """Context manager yielding a SQLModel Session with autocommit."""
    db = Session(engine)
    try:
        yield db
        db.commit()
    except Exception:
        db.rollback()
        raise
    finally:
        db.close()
