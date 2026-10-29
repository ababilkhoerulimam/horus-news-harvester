"""Shared SQLite connection setup for the WAL-backed stores."""

import sqlite3
from contextlib import contextmanager
from typing import Generator, Iterable


@contextmanager
def connect_wal(
    db_path,
    timeout: float = 30.0,
    row_factory=None,
    extra_pragmas: Iterable[str] = (),
) -> Generator[sqlite3.Connection, None, None]:
    """Opens a connection with WAL journaling and normal synchronous mode.

    Callers that need a different timeout, row factory, or extra tuning pass it
    explicitly so the shared defaults stay predictable.
    """
    conn = sqlite3.connect(str(db_path), timeout=timeout)
    conn.execute("PRAGMA journal_mode = WAL;")
    conn.execute("PRAGMA synchronous = NORMAL;")
    for pragma in extra_pragmas:
        conn.execute(pragma)
    if row_factory is not None:
        conn.row_factory = row_factory
    try:
        with conn:
            yield conn
    finally:
        conn.close()
