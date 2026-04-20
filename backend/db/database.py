from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from backend.config import AppConfig, resolve_path


SCHEMA_PATH = Path(__file__).with_name("schema.sql")


def init_db(config: AppConfig) -> Path:
    db_path = resolve_path(config.db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    with sqlite3.connect(db_path) as conn:
        # WAL gives us concurrent readers during writes (Telegram polling
        # thread + FastAPI requests hit the same DB). synchronous=NORMAL is
        # the documented safe companion to WAL for application-level crash
        # recovery. journal_mode is persistent in the DB header, so setting
        # it once on init is sufficient.
        conn.execute("PRAGMA journal_mode = WAL")
        conn.execute("PRAGMA synchronous = NORMAL")
        conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
        conn.commit()
    return db_path


@contextmanager
def connect(config: AppConfig) -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(resolve_path(config.db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()

