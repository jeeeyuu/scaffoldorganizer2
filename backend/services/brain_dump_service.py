"""Structure a raw brain dump into a session + per-item rows.

Shared by two paths:
- `POST /brain-dump/structure` — explicit structurer button in the GUI.
- `POST /chat/command` — when the classifier flags `decompose_as_brain_dump`
  and the router hands the text over.

Contract with task_structurer is JSON: it returns `{items: [...], summary,
structured_markdown}`. We iterate items directly — no more server-side
Markdown parsing to recover them. The Markdown parser stays in the tree
as a best-effort fallback for legacy / malformed outputs.
"""
from __future__ import annotations

from typing import Any

from backend.config import AppConfig
from backend.db.database import connect
from backend.models.schemas import ItemCreate, SessionSave, Source
from backend.services.ai_service import AIService
from backend.services.brain_dump_parser import parse_brain_dump_markdown
from backend.services.item_service import create_item, list_items
from backend.services.session_service import save_session


# Inbox-first policy: every item produced from a chat-driven brain dump
# lands in Inbox so the user decides when to promote it to Active. This
# matches the single-item chat flow (see router_service.handle_chat_command)
# and the top-level spec — Inbox is for "uncommitted captures". The one
# exception is horizon=long_term: the user (or structurer) has already
# committed the item to the long-term backlog, so it goes straight there
# with status=todo.
_DEFAULT_HORIZON = "now"


def structure_and_create(
    config: AppConfig,
    ai: AIService,
    raw_text: str,
    title: str = "Brain dump",
    source: Source = "brain_dump",
) -> dict[str, Any]:
    # 1) Fetch context in a short read-only transaction before the LLM
    # call so we don't hold the DB connection during the multi-second
    # request.
    with connect(config) as conn:
        active_tasks = list_items(conn, status="todo")
        doing_tasks = list_items(conn, status="doing")
        long_term_items = list_items(conn, horizon="long_term")

    # 2) LLM call — no DB connection held.
    structured = ai.structure_brain_dump(
        raw_text,
        active_tasks=[*active_tasks, *doing_tasks],
        long_term_items=long_term_items,
    )

    # 3) Gather items. Primary source is the JSON `items` array returned
    # by task_structurer. If that's empty but we got Markdown (legacy
    # output or offline fallback), try parsing the Markdown as a last
    # resort so we still produce something useful.
    parsed_items = list(structured.get("items") or [])
    markdown = str(structured.get("structured_text") or "")
    if not parsed_items and markdown:
        parsed_items = parse_brain_dump_markdown(markdown)

    # 4) Persist session + items in a single short transaction.
    created: list[dict] = []
    with connect(config) as conn:
        session = save_session(
            conn,
            SessionSave(
                title=title,
                raw_text=raw_text,
                structured_text=markdown,
            ),
        )
        for parsed_item in parsed_items:
            created.append(create_item(conn, _build_item(parsed_item, session["id"], source)))
    return {"session": session, "structured": structured, "items": created}


def _build_item(parsed: dict[str, Any], session_id: int, source: Source) -> ItemCreate:
    """Map a structurer JSON item (or legacy parser dict) to ItemCreate.

    Status is NOT taken from the structurer — instead we derive it
    backend-side to keep the Inbox-first policy consistent. The
    structurer's suggested_status (if any) is treated as a hint only.
    """
    horizon = parsed.get("horizon") or _DEFAULT_HORIZON
    item_type = parsed.get("item_type") or "task"

    # Inbox by default. Horizon=long_term skips Inbox and goes directly
    # to the Long-term backlog (status=todo, horizon=long_term) — same
    # rule the single-item chat-create path applies for item_kind=long_term.
    if horizon == "long_term":
        status = "todo"
    else:
        status = "inbox"

    kwargs: dict[str, Any] = {
        "item_type": item_type,
        "title": (parsed.get("title") or parsed.get("content") or "")[:120] or "Untitled",
        "content": parsed.get("content") or parsed.get("title") or "",
        "status": status,
        "horizon": horizon,
        "source": source,
        "session_id": session_id,
    }
    priority = parsed.get("priority")
    if isinstance(priority, int) and 1 <= priority <= 5:
        kwargs["priority"] = priority
    project = parsed.get("project")
    if isinstance(project, str) and project:
        kwargs["project"] = project
    tags = parsed.get("tags")
    if isinstance(tags, list) and tags:
        kwargs["tags"] = [str(t) for t in tags if t]
    return ItemCreate(**kwargs)
