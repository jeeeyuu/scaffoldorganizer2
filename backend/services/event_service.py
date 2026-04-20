from __future__ import annotations

import json
import sqlite3
from typing import Any

from backend.utils.time import utc_now


def record_event(
    conn: sqlite3.Connection,
    event_type: str,
    item_id: int | None = None,
    session_id: int | None = None,
    from_status: str | None = None,
    to_status: str | None = None,
    payload: dict[str, Any] | None = None,
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO events (
            item_id, session_id, event_type, from_status, to_status, payload_json, created_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item_id,
            session_id,
            event_type,
            from_status,
            to_status,
            json.dumps(payload or {}, ensure_ascii=False),
            utc_now(),
        ),
    )
    return int(cursor.lastrowid)

