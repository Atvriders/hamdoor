import logging
import os

from sqlalchemy import create_engine, event
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from app.config import get_settings


log = logging.getLogger(__name__)

BUSY_TIMEOUT_S = 15


class Base(DeclarativeBase):
    pass


def _make_engine():
    url = get_settings().database_url
    if url.startswith("sqlite"):
        # sqlite path from URL, e.g. sqlite:////data/hamdoor.db -> /data/hamdoor.db
        path = url.split("///", 1)[-1]
        if path and path != ":memory:":
            os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        # a request waits up to 15 s for another writer (the importer's
        # transactions are kept far shorter than that)
        return create_engine(url, connect_args={"check_same_thread": False, "timeout": BUSY_TIMEOUT_S})
    return create_engine(url)


engine = _make_engine()


@event.listens_for(engine, "connect")
def _sqlite_pragma(dbapi_conn, _):
    try:
        cur = dbapi_conn.cursor()
        cur.execute("PRAGMA foreign_keys=ON")
        # in WAL mode (set once by init_db, kept in the file) NORMAL is
        # durable against crashes and much cheaper per commit than FULL
        if str(cur.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal":
            cur.execute("PRAGMA synchronous=NORMAL")
        cur.close()
    except Exception:
        pass  # non-sqlite backends


def enable_wal(bind=None) -> str:
    """Put the SQLite file in write-ahead-log mode (persistent, so once is
    enough; harmless when it already is). Readers then never block writers:
    in the old rollback-journal mode the importer's long read of the hams
    table made every save in the app fail with 'database is locked'.
    SQLITE_WAL=false keeps the rollback journal (for a data directory on a
    network or FUSE filesystem, where WAL's shared memory can't work); a
    file that refuses WAL stays as it is. Returns the journal mode now in
    effect."""
    eng = bind or engine
    if eng.dialect.name != "sqlite":
        return ""
    want = "WAL" if get_settings().sqlite_wal else "DELETE"
    try:
        with eng.connect() as conn:
            mode = str(conn.exec_driver_sql(f"PRAGMA journal_mode={want}").scalar() or "").lower()
    except Exception:
        log.warning("[db] could not set journal_mode=%s; keeping the current mode", want, exc_info=True)
        return ""
    if mode != want.lower():
        log.warning("[db] SQLite kept journal_mode=%s (asked for %s)", mode, want)
    return mode


SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def init_db():
    from app import models  # noqa: F401  (register models)

    enable_wal()
    Base.metadata.create_all(bind=engine)
