from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.config import AppConfig, resolve_path
from backend.models.schemas import SessionSave
from backend.services.event_service import record_event
from backend.utils.time import utc_now


def save_session(conn: sqlite3.Connection, payload: SessionSave) -> dict:
    now = utc_now()
    cursor = conn.execute(
        """
        INSERT INTO sessions (title, raw_text, structured_text, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?)
        """,
        (payload.title.strip() or "Untitled session", payload.raw_text, payload.structured_text, now, now),
    )
    session_id = int(cursor.lastrowid)
    record_event(conn, "session_saved", session_id=session_id)
    return get_session(conn, session_id) or {}


def list_sessions(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM sessions ORDER BY updated_at DESC").fetchall()
    return [dict(row) for row in rows]


def get_session(conn: sqlite3.Connection, session_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM sessions WHERE id = ?", (session_id,)).fetchone()
    if row is None:
        return None
    data = dict(row)
    item_rows = conn.execute(
        "SELECT * FROM items WHERE session_id = ? ORDER BY created_at ASC",
        (session_id,),
    ).fetchall()
    data["items"] = [dict(item) for item in item_rows]
    return data


def export_session_markdown(conn: sqlite3.Connection, config: AppConfig, session_id: int) -> Path:
    session = get_session(conn, session_id)
    if session is None:
        raise ValueError("Session not found")
    export_dir = resolve_path(config.session_export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"session_{session_id}.md"
    lines = [
        f"# {session['title']}",
        "",
        "## Raw",
        session.get("raw_text") or "",
        "",
        "## Structured",
        session.get("structured_text") or "",
    ]
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    conn.execute(
        "UPDATE sessions SET export_md_path = ?, updated_at = ? WHERE id = ?",
        (str(path), utc_now(), session_id),
    )
    record_event(conn, "exported_md", session_id=session_id, payload={"path": str(path)})
    return path

