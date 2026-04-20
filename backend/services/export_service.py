from __future__ import annotations

import sqlite3
from pathlib import Path

from backend.config import AppConfig, resolve_path
from backend.services.event_service import record_event
from backend.services.item_service import list_items
from backend.utils.time import local_date


def export_items_markdown(conn: sqlite3.Connection, config: AppConfig) -> Path:
    export_dir = resolve_path(config.markdown_export_dir)
    export_dir.mkdir(parents=True, exist_ok=True)
    path = export_dir / f"items_{local_date()}.md"
    items = list_items(conn)
    lines = ["# ScaffoldOrganizer 2.0 Items", ""]
    for status in ["doing", "todo", "inbox", "done", "archived"]:
        section_items = [item for item in items if item["status"] == status]
        if not section_items:
            continue
        lines.extend([f"## {status.title()}", ""])
        for item in section_items:
            tags = " ".join(f"#{tag}" for tag in item.get("tags", []))
            lines.append(f"- [{item['horizon']}] P{item['priority']} {item['title']} {tags}".rstrip())
        lines.append("")
    path.write_text("\n".join(lines).strip() + "\n", encoding="utf-8")
    record_event(conn, "exported_md", payload={"path": str(path), "scope": "items"})
    return path

