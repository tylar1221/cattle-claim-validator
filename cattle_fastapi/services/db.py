import os

import psycopg2.extras
from dotenv import load_dotenv
from sqlalchemy import create_engine

from services.models import Base

load_dotenv()

# Same pool settings as your old project
engine = create_engine(
    os.environ["DATABASE_URL"],
    pool_pre_ping=True,
    pool_size=10,
    max_overflow=20,
)

from contextlib import contextmanager

class _Conn:
    def __init__(self, raw):
        self._raw = raw

    def execute(self, sql, params=()):
        cur = self._raw.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(sql.replace("?", "%s"), params)
        return cur

    def commit(self):
        self._raw.commit()

    def rollback(self):
        self._raw.rollback()

    def close(self):
        self._raw.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        if exc_type is not None:
            try:
                self._raw.rollback()
            except Exception:
                pass
        self._raw.close()
        return False


@contextmanager
def db_conn():
    """Use this instead of get_conn() everywhere. Auto-closes + rolls back on error."""
    c = _Conn(engine.raw_connection())
    try:
        yield c
    finally:
        c.close()


def get_conn():
    """Legacy -- still works but leaks on error. Migrate callers to db_conn()."""
    return _Conn(engine.raw_connection())

def init_db():
    # Creates the cases + captures tables in RDS if they don't exist yet.
    # Replaces the whole old CREATE TABLE + ALTER TABLE block.
    Base.metadata.create_all(bind=engine)