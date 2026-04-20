from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from backend.config import AppConfig, resolve_path
from backend.db.database import connect
from backend.services.ai_service import AIService
from backend.services.event_service import record_event
from backend.utils.time import local_date, utc_now


def assemble_worklog_context(conn: sqlite3.Connection, log_date: str | None = None) -> dict:
    day = log_date or local_date()
    pattern = f"{day}%"
    events = [dict(row) for row in conn.execute(
        "SELECT * FROM events WHERE created_at LIKE ? ORDER BY created_at ASC",
        (pattern,),
    ).fetchall()]
    items = [dict(row) for row in conn.execute(
        "SELECT * FROM items WHERE created_at LIKE ? OR updated_at LIKE ? OR completed_at LIKE ? ORDER BY updated_at ASC",
        (pattern, pattern, pattern),
    ).fetchall()]
    sessions = [dict(row) for row in conn.execute(
        "SELECT * FROM sessions WHERE created_at LIKE ? OR updated_at LIKE ? ORDER BY updated_at ASC",
        (pattern, pattern),
    ).fetchall()]
    return {
        "log_date": day,
        "events": events,
        "created": [item for item in items if str(item.get("created_at", "")).startswith(day)],
        "started": [event for event in events if event.get("event_type") == "status_changed" and event.get("to_status") == "doing"],
        "doing": [item for item in items if item.get("status") == "doing"],
        "completed": [item for item in items if str(item.get("completed_at") or "").startswith(day)],
        "thoughts": [item for item in items if item.get("item_type") in {"thought", "journal_seed", "note"}],
        "sessions": sessions,
        "next_actions": [item for item in items if item.get("status") in {"todo", "doing"}],
    }


def _slim_summary(context: dict) -> dict[str, Any]:
    """Compact representation to persist alongside a worklog row.

    The full context (with event payloads and item bodies) can easily reach
    tens of KB per worklog. We only need the pointers to reconstruct what the
    worklog was based on; full reconstruction should re-query the DB.
    """

    def _ids(rows: list[dict]) -> list[int]:
        return [int(r["id"]) for r in rows if isinstance(r, dict) and r.get("id") is not None]

    def _titles(rows: list[dict]) -> list[str]:
        return [str(r.get("title") or r.get("event_type") or r.get("id", "")) for r in rows]

    return {
        "log_date": context.get("log_date"),
        "item_ids": _ids(context.get("created", []) + context.get("doing", []) + context.get("completed", []) + context.get("thoughts", [])),
        "session_ids": _ids(context.get("sessions", [])),
        "event_ids": _ids(context.get("events", [])),
        "started_titles": _titles(context.get("started", [])),
        "completed_titles": _titles(context.get("completed", [])),
    }


def generate_worklog_draft(config: AppConfig, ai: AIService, log_date: str | None = None) -> dict:
    """Produce a worklog draft via LLM without touching the DB.

    The GUI calls this on the Work-Log button press. The draft is shown to
    the user for review; persistence happens only after they click Save.
    Keeping generate and save separate means the user can re-generate
    without piling up unwanted rows in the worklogs table.
    """
    with connect(config) as conn:
        context = assemble_worklog_context(conn, log_date)
    result = ai.write_worklog(context)
    summary = _slim_summary(context)
    return {
        "log_date": context["log_date"],
        "title": f"Work Log {context['log_date']}",
        "content_md": result.get("content_md", ""),
        "used_fallback": bool(result.get("used_fallback", False)),
        "context_summary": summary,
    }


def save_worklog_draft(
    config: AppConfig,
    log_date: str,
    title: str,
    content_md: str,
    context_summary: dict | None = None,
) -> dict:
    """Persist a (possibly edited) worklog draft into the DB."""
    now = utc_now()
    with connect(config) as conn:
        cursor = conn.execute(
            """
            INSERT INTO worklogs (log_date, title, content_md, source_summary_json, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (
                log_date,
                title,
                content_md,
                json.dumps(context_summary or {}, ensure_ascii=False),
                now,
                now,
            ),
        )
        worklog_id = int(cursor.lastrowid)
        record_event(
            conn,
            "worklog_saved",
            payload={"worklog_id": worklog_id, "log_date": log_date},
        )
    return {
        "id": worklog_id,
        "title": title,
        "content_md": content_md,
        "log_date": log_date,
    }


def list_worklogs(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute("SELECT * FROM worklogs ORDER BY log_date DESC, created_at DESC").fetchall()
    return [dict(row) for row in rows]


def export_worklog(conn: sqlite3.Connection, config: AppConfig, worklog_id: int) -> Path:
    row = conn.execute("SELECT * FROM worklogs WHERE id = ?", (worklog_id,)).fetchone()
    if row is None:
        raise ValueError("Worklog not found")
    export_dir = resolve_path(config.worklog_export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"worklog_{row['log_date']}_{worklog_id}.md"
    path.write_text(row["content_md"], encoding="utf-8")
    record_event(conn, "exported_md", payload={"path": str(path), "worklog_id": worklog_id})
    return path
