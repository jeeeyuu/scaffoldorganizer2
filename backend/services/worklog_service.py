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
    """Collect the events, items and sessions that should feed the next
    worklog draft.

    Two modes:

    * **explicit date** — caller passes `log_date="YYYY-MM-DD"`. We cover
      that calendar day's full span (00:00 → 23:59 UTC).
    * **default / "since last log"** — caller passes nothing. We cover
      everything since the most recent `worklogs` row was created, up to
      `now`. If no prior worklog exists we fall back to the start of
      today. This is what the GUI's Work Log button uses, so successive
      drafts don't double-report the same activity.
    """

    now = utc_now()
    if log_date:
        day = log_date
        window_start = f"{day}T00:00:00+00:00"
        window_end = f"{day}T23:59:59+00:00"
    else:
        day = local_date()
        prev = conn.execute(
            "SELECT created_at FROM worklogs ORDER BY created_at DESC LIMIT 1"
        ).fetchone()
        window_start = str(prev["created_at"]) if prev else f"{day}T00:00:00+00:00"
        window_end = now

    events = [dict(row) for row in conn.execute(
        "SELECT * FROM events WHERE created_at >= ? AND created_at <= ? ORDER BY created_at ASC",
        (window_start, window_end),
    ).fetchall()]
    items = [dict(row) for row in conn.execute(
        "SELECT * FROM items "
        "WHERE (created_at   >= ? AND created_at   <= ?) "
        "   OR (updated_at   >= ? AND updated_at   <= ?) "
        "   OR (completed_at >= ? AND completed_at <= ?) "
        "ORDER BY updated_at ASC",
        (window_start, window_end) * 3,
    ).fetchall()]
    sessions = [dict(row) for row in conn.execute(
        "SELECT * FROM sessions "
        "WHERE (created_at >= ? AND created_at <= ?) "
        "   OR (updated_at >= ? AND updated_at <= ?) "
        "ORDER BY updated_at ASC",
        (window_start, window_end) * 2,
    ).fetchall()]

    def _in_window(ts: object) -> bool:
        if not ts:
            return False
        return window_start <= str(ts) <= window_end

    return {
        "log_date": day,
        "window_start": window_start,
        "window_end": window_end,
        "events": events,
        "created":   [i for i in items if _in_window(i.get("created_at"))],
        "started":   [e for e in events if e.get("event_type") == "status_changed" and e.get("to_status") == "doing"],
        "doing":     [i for i in items if i.get("status") == "doing"],
        "completed": [i for i in items if _in_window(i.get("completed_at"))],
        "thoughts":  [i for i in items if i.get("item_type") in {"thought", "journal_seed", "note"}],
        "sessions":  sessions,
        "next_actions": [i for i in items if i.get("status") in {"todo", "doing"}],
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
