from __future__ import annotations

from pydantic import ValidationError

from backend.config import AppConfig
from backend.db.database import connect
from backend.models.schemas import ChatCommand, ItemCreate, ItemUpdate, RouterResult
from backend.services.ai_service import AIService
from backend.services.brain_dump_service import structure_and_create
from backend.services.item_service import change_status, create_item, delete_items, update_item


def _first_line(text: str) -> str:
    stripped = text.strip()
    return stripped.splitlines()[0][:80] if stripped else "Brain dump"


def _dispatch_structure_brain_dump(
    config: AppConfig,
    ai: AIService,
    text: str,
    title: str,
) -> dict:
    """Run the structurer path and shape the response like a router result.

    The classifier gates entry into this path — it returns
    `decompose_as_brain_dump: true` when the input carries multiple tasks
    regardless of how short the text is (e.g. "A 끝내고 B 정리하고 C 마감").
    """
    result = structure_and_create(
        config,
        ai,
        raw_text=text,
        title=title,
        source="chat_input",
    )
    item_count = len(result.get("items", []))
    return {
        "router": {
            "mode": "content_capture",
            "actions": [{"type": "structure_brain_dump"}],
            "user_feedback": (
                f"Classifier flagged multi-item input → session #{result['session']['id']} "
                f"+ {item_count} item{'' if item_count == 1 else 's'}"
            ),
        },
        "results": [{"action": "structure_brain_dump", **result}],
    }


def handle_chat_command(config: AppConfig, ai: AIService, command: ChatCommand) -> dict:
    # 1) Router LLM call decides the action plan (command vs content capture).
    raw = ai.route(
        command.text,
        selected_item_ids=command.selected_item_ids,
        selected_items=command.selected_items,
        active_session=command.active_session,
        ui_context=command.ui_context.model_dump(),
    )
    try:
        routed = RouterResult.model_validate(raw)
    except ValidationError:
        routed = RouterResult(
            mode="capture",
            actions=[{"type": "create_item", "content": command.text}],
            user_feedback="Router response was invalid; captured as inbox item.",
        )

    # 2) If the router itself emits `structure_brain_dump` (future-proof —
    # the dashboard prompt may learn this action later), short-circuit.
    for action in routed.actions:
        if action.type == "structure_brain_dump":
            content = action.content or command.text
            return _dispatch_structure_brain_dump(config, ai, content, _first_line(content))

    # 3) For create_item / classify_and_store actions, call the classifier.
    # Per the user's spec, the classifier is the gate that decides whether
    # this captured text becomes a single item or gets handed to the
    # task_structurer for decomposition. No length-based heuristic here —
    # classifier judges semantic multiplicity (see classifier prompt spec).
    classify_cache: dict[str, dict] = {}
    for action in routed.actions:
        if action.type in {"create_item", "classify_and_store"}:
            content = action.content or command.text
            if content not in classify_cache:
                classification = ai.classify(content, source="chat_input")
                classify_cache[content] = classification
            else:
                classification = classify_cache[content]
            if classification.get("decompose_as_brain_dump"):
                return _dispatch_structure_brain_dump(
                    config, ai, content, _first_line(content),
                )

    # 4) Apply all non-decomposition actions in a single short DB transaction.
    results: list[dict] = []
    with connect(config) as conn:
        for action in routed.actions:
            if action.type in {"create_item", "classify_and_store"}:
                content = action.content or command.text
                classification = classify_cache[content]
                # Chat captures land in Inbox for user triage — the
                # classifier's `suggested_status` is a hint, not a commit.
                # The only exception is an explicit long-term kind, which
                # bypasses Inbox because the user has already committed to
                # keeping it in the long-term backlog.
                if action.item_kind == "long_term":
                    target_status: str = "todo"
                    target_horizon: str = "long_term"
                else:
                    target_status = "inbox"
                    target_horizon = classification.get("horizon", "now")
                item = create_item(
                    conn,
                    ItemCreate(
                        item_type=classification.get("item_type", "task"),
                        title=classification.get("title") or content[:120],
                        content=classification.get("content") or content,
                        status=target_status,
                        horizon=target_horizon,
                        priority=int(classification.get("priority", 3)),
                        source="chat_input",
                        project=classification.get("project", ""),
                        tags=classification.get("tags", []),
                    ),
                )
                results.append({"action": action.type, "item": item})
            elif action.type in {"mark_doing", "mark_selected_item_doing"}:
                for item_id in _target_item_ids(action.item_id, command.selected_item_ids):
                    results.append({"action": action.type, "item": change_status(conn, item_id, "doing")})
            elif action.type in {"mark_done", "mark_selected_item_done"}:
                for item_id in _target_item_ids(action.item_id, command.selected_item_ids):
                    results.append({"action": action.type, "item": change_status(conn, item_id, "done")})
            elif action.type in {"move_to_long_term", "move_selected_item_to_long_term"}:
                for item_id in _target_item_ids(action.item_id, command.selected_item_ids):
                    results.append(
                        {
                            "action": action.type,
                            "item": update_item(conn, item_id, ItemUpdate(horizon="long_term", status="todo")),
                        }
                    )
            elif action.type in {"archive_selected_item", "archive_item"}:
                for item_id in _target_item_ids(action.item_id, command.selected_item_ids):
                    results.append(
                        {
                            "action": action.type,
                            "item": change_status(conn, item_id, "archived"),
                        }
                    )
            elif action.type in {"delete_selected_item", "delete_item"}:
                # HARD delete — row is gone after this. Router prompt rule
                # 8 only emits this when the user explicitly said "완전히
                # 지워" / "permanently delete" / "영구" etc.
                target_ids = _target_item_ids(action.item_id, command.selected_item_ids)
                deleted = delete_items(conn, target_ids)
                results.append({"action": action.type, "deleted": deleted, "ids": target_ids})
            elif action.type in {"reprioritize_items", "set_priority"}:
                priority = _coerce_priority(action.payload.get("priority"))
                if priority is None:
                    results.append({"action": action.type, "status": "missing_or_invalid_priority"})
                    continue
                for item_id in _target_item_ids(action.item_id, command.selected_item_ids):
                    results.append(
                        {
                            "action": action.type,
                            "item": update_item(conn, item_id, ItemUpdate(priority=priority)),
                        }
                    )
            elif action.type == "move_selected_item" and command.selected_item_ids:
                target_status = action.status or action.payload.get("status")
                target_horizon = action.payload.get("horizon")
                for item_id in command.selected_item_ids:
                    results.append(
                        {
                            "action": action.type,
                            "item": update_item(
                                conn,
                                item_id,
                                ItemUpdate(status=target_status, horizon=target_horizon),
                            ),
                        }
                    )
            elif action.type == "update_item_fields":
                # Accept either explicit action.item_id or fall back to the
                # current selection — router may emit one action per id or
                # a single "update selected" action.
                target_ids = _target_item_ids(action.item_id, command.selected_item_ids)
                if not target_ids:
                    results.append({"action": action.type, "status": "no_target"})
                    continue
                patch = ItemUpdate.model_validate(action.payload)
                for item_id in target_ids:
                    results.append(
                        {
                            "action": action.type,
                            "item": update_item(conn, item_id, patch),
                        }
                    )
            else:
                results.append({"action": action.type, "status": "queued_or_unsupported"})
    return {"router": routed.model_dump(), "results": results}


def _target_item_ids(item_id: int | None, selected_item_ids: list[int]) -> list[int]:
    if item_id is not None:
        return [item_id]
    return selected_item_ids


def _coerce_priority(value: object) -> int | None:
    """Normalise a priority value coming from router action.payload.

    Accepts ints (1..5), "P3"-style strings, or numeric strings. Returns
    None for anything else so the caller can short-circuit.
    """
    if value is None:
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, int) and 1 <= value <= 5:
        return value
    if isinstance(value, str):
        stripped = value.strip().lstrip("Pp")
        try:
            parsed = int(stripped)
            if 1 <= parsed <= 5:
                return parsed
        except ValueError:
            return None
    return None
