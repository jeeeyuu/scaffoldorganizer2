from __future__ import annotations

import json
import sqlite3

from backend.models.schemas import ItemCreate, ItemUpdate
from backend.services.event_service import record_event
from backend.utils.time import utc_now


VALID_STATUSES = {"inbox", "todo", "doing", "done", "archived"}


def _row_to_item(row: sqlite3.Row) -> dict:
    data = dict(row)
    data["tags"] = json.loads(data.pop("tags_json") or "[]")
    return data


def create_item(conn: sqlite3.Connection, item: ItemCreate) -> dict:
    now = utc_now()
    completed_at = now if item.status == "done" else None
    cursor = conn.execute(
        """
        INSERT INTO items (
            item_type, title, content, status, horizon, priority, source, project,
            tags_json, scheduled_date, due_date, created_at, updated_at, completed_at,
            session_id, external_ref
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            item.item_type,
            item.title.strip(),
            item.content.strip(),
            item.status,
            item.horizon,
            item.priority,
            item.source,
            item.project,
            json.dumps(item.tags, ensure_ascii=False),
            item.scheduled_date,
            item.due_date,
            now,
            now,
            completed_at,
            item.session_id,
            item.external_ref,
        ),
    )
    item_id = int(cursor.lastrowid)
    record_event(
        conn,
        "created",
        item_id=item_id,
        session_id=item.session_id,
        to_status=item.status,
        payload={"source": item.source, "horizon": item.horizon, "item_type": item.item_type},
    )
    return get_item(conn, item_id) or {}


def list_items(
    conn: sqlite3.Connection,
    status: str | None = None,
    horizon: str | None = None,
    item_type: str | None = None,
    exclude_horizon: str | None = None,
) -> list[dict]:
    clauses: list[str] = []
    values: list[str] = []
    if status:
        # A comma-separated value means "IN (...)". This lets the GUI's
        # Active tab request todo+doing in a single call (user wants them
        # on one page to triage next moves).
        statuses = [s.strip() for s in status.split(",") if s.strip()]
        if len(statuses) == 1:
            clauses.append("status = ?")
            values.append(statuses[0])
        elif statuses:
            placeholders = ",".join(["?"] * len(statuses))
            clauses.append(f"status IN ({placeholders})")
            values.extend(statuses)
    if horizon:
        clauses.append("horizon = ?")
        values.append(horizon)
    if exclude_horizon:
        clauses.append("horizon != ?")
        values.append(exclude_horizon)
    if item_type:
        clauses.append("item_type = ?")
        values.append(item_type)
    where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
    rows = conn.execute(
        f"SELECT * FROM items {where} ORDER BY status = 'doing' DESC, priority ASC, created_at DESC",
        values,
    ).fetchall()
    return [_row_to_item(row) for row in rows]


def get_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    row = conn.execute("SELECT * FROM items WHERE id = ?", (item_id,)).fetchone()
    return _row_to_item(row) if row else None


def update_item(conn: sqlite3.Connection, item_id: int, patch: ItemUpdate) -> dict | None:
    current = get_item(conn, item_id)
    if current is None:
        return None
    values = patch.model_dump(exclude_unset=True)
    if "tags" in values:
        values["tags_json"] = json.dumps(values.pop("tags"), ensure_ascii=False)
    if not values:
        return current
    values["updated_at"] = utc_now()
    if values.get("status") == "done" and current["status"] != "done":
        values["completed_at"] = values["updated_at"]
    assignments = ", ".join(f"{key} = ?" for key in values)
    conn.execute(
        f"UPDATE items SET {assignments} WHERE id = ?",
        [*values.values(), item_id],
    )
    record_event(conn, "updated", item_id=item_id, payload={"changed": sorted(values)})
    if "status" in values and values["status"] != current["status"]:
        record_event(
            conn,
            "status_changed",
            item_id=item_id,
            from_status=current["status"],
            to_status=values["status"],
        )
    return get_item(conn, item_id)


def change_status(conn: sqlite3.Connection, item_id: int, status: str) -> dict | None:
    if status not in VALID_STATUSES:
        raise ValueError(f"Invalid status: {status}")
    return update_item(conn, item_id, ItemUpdate(status=status))

def archive_item(conn: sqlite3.Connection, item_id: int) -> dict | None:
    return change_status(conn, item_id, "archived")


def reset_all_items(conn: sqlite3.Connection) -> int:
    """Hard-delete every row in `items`. Returns the number deleted.

    The reset is destructive and includes long-term items. Event history is
    kept but item_id references are NULLed first so the FK does not block
    the DELETE — this preserves the audit trail of what happened, even
    after the underlying items are gone.
    """
    count = int(conn.execute("SELECT COUNT(*) FROM items").fetchone()[0])
    conn.execute("UPDATE events SET item_id = NULL WHERE item_id IS NOT NULL")
    conn.execute("DELETE FROM items")
    record_event(conn, "items_reset", payload={"count": count})
    return count


def delete_items(conn: sqlite3.Connection, item_ids: list[int]) -> int:
    """Hard-delete the specified item rows. Returns the number actually
    removed. Events referencing these items are NULLed (not deleted) so
    the audit trail survives — callers that want full erasure can prune
    events separately.

    Use archive_item() (soft delete, status=archived) for reversible
    hides. This function is reserved for intents like "완전히 지워줘",
    "delete permanently", etc. that explicitly want the row gone.
    """
    if not item_ids:
        return 0
    ids = [int(i) for i in item_ids]
    placeholders = ",".join(["?"] * len(ids))
    conn.execute(
        f"UPDATE events SET item_id = NULL WHERE item_id IN ({placeholders})",
        ids,
    )
    cursor = conn.execute(
        f"DELETE FROM items WHERE id IN ({placeholders})",
        ids,
    )
    deleted = int(cursor.rowcount)
    record_event(conn, "items_deleted", payload={"ids": ids, "count": deleted})
    return deleted

