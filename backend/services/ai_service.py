from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from backend.config import AIRoleConfig, AppConfig, ROOT_DIR
from backend.utils.time import local_date, utc_now

log = logging.getLogger(__name__)


# Only used when a role has no prompt_id configured (offline/dev path).
# In production the dashboard prompt owns the model.
_FALLBACK_MODEL = "gpt-4.1-mini"


# Fields included when passing existing items as LLM context. Kept minimal
# because sending full row dicts (with content bodies, tags, timestamps) is
# the single biggest source of token waste in brain-dump and routing calls.
_DIGEST_FIELDS = ("id", "title", "horizon", "status", "priority", "project")


def digest_items(items: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    if not items:
        return []
    return [
        {k: item.get(k) for k in _DIGEST_FIELDS if item.get(k) not in (None, "")}
        for item in items
    ]


class AIService:
    """Thin AI boundary.

    Each role is configured independently (model / prompt_id / prompt_file /
    response_format). When a role has `prompt_id`, the OpenAI Responses API's
    server-managed prompt is used (the prompt body lives on OpenAI's side and
    is referenced by ID). When no `prompt_id` is configured, the local
    Markdown file under `backend/prompts/` is sent as the system message —
    this keeps local development and CI paths working without dashboard
    registration.

    All LLM-facing methods have a deterministic fallback so the app remains
    usable without an API key.
    """

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self.last_error: str = ""
        self._client: Any = None
        self._prompt_cache: dict[str, str] = {}

    @property
    def ready(self) -> bool:
        return bool(self.config.openai_api_key.strip())

    # ------------------------------------------------------------------ core

    def _role(self, name: str) -> AIRoleConfig:
        return self.config.role(name)

    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        from openai import OpenAI

        self._client = OpenAI(api_key=self.config.openai_api_key)
        return self._client

    def _load_prompt_file(self, role: AIRoleConfig) -> str:
        if not role.prompt_file:
            return ""
        if role.prompt_file in self._prompt_cache:
            return self._prompt_cache[role.prompt_file]
        path = Path(role.prompt_file)
        if not path.is_absolute():
            path = ROOT_DIR / path
        text = path.read_text(encoding="utf-8") if path.exists() else ""
        self._prompt_cache[role.prompt_file] = text
        return text

    def _call(self, role_name: str, user_message: str) -> str:
        """Single OpenAI Responses API call. Returns raw output_text.

        We send the smallest possible payload — everything else lives on
        the OpenAI dashboard under the prompt_id. In particular,
        `temperature`, `max_output_tokens`, and `model` are NOT forwarded:
        some models (o-series, reasoning) 400 on `temperature`, and
        overriding the dashboard's model from here just creates drift.
        """

        role = self._role(role_name)
        client = self._get_client()

        if role.prompt_id:
            response = client.responses.create(
                prompt={"id": role.prompt_id},
                input=[{"role": "user", "content": user_message}],
            )
        else:
            # Offline / dev fallback — no dashboard prompt to read from,
            # so we have to supply both the developer message and a model.
            # This path exists to keep `make test` and first-run
            # exploration working without having to register prompts. In
            # production every role has a prompt_id and this branch is dead.
            developer_prompt = self._load_prompt_file(role)
            response = client.responses.create(
                model=_FALLBACK_MODEL,
                input=[
                    {"role": "developer", "content": developer_prompt},
                    {"role": "user", "content": user_message},
                ],
            )
        return getattr(response, "output_text", "") or ""

    # -------------------------------------------------------------- formatters
    #
    # Each role's user-message format mirrors what's registered in the OpenAI
    # dashboard (see api_prompt.md). Sending a different shape than the
    # prompt was tested with would measurably degrade output quality.

    @staticmethod
    def _format_classifier_input(text: str) -> str:
        return f"[INPUT]\n{text.strip()}\n"

    @staticmethod
    def _format_router_input(
        text: str,
        selected_items: list[dict[str, Any]],
        active_session: dict[str, Any] | None,
        ui_context: dict[str, Any] | None,
    ) -> str:
        context = {
            "timestamp": utc_now(),
            "selected_item": selected_items[0] if selected_items else None,
            "selected_items": selected_items,
            "active_session": active_session,
            "ui_context": ui_context or {},
        }
        context_json = json.dumps(context, ensure_ascii=False, indent=2)
        return f"[CONTEXT]\n{context_json}\n\n[USER INPUT]\n{text.strip()}\n"

    @staticmethod
    def _format_structurer_input(
        raw_text: str,
        active_tasks: list[dict[str, Any]],
        long_term_items: list[dict[str, Any]],
    ) -> str:
        def _lines(items: list[dict[str, Any]]) -> str:
            if not items:
                return "(none)"
            return "\n".join(f"- {item.get('title', '')}".rstrip() for item in items if item.get("title"))

        return (
            f"[DATE]\n{local_date()}\n\n"
            f"[ACTIVE TASKS]\n{_lines(active_tasks)}\n\n"
            f"[LONG TERM]\n{_lines(long_term_items)}\n\n"
            f"[BRAIN DUMP]\n{raw_text.strip()}\n"
        )

    @staticmethod
    def _format_worklog_input(context: dict[str, Any]) -> str:
        payload = {
            "date": context.get("log_date") or local_date(),
            "started_tasks": _labels(context.get("started", [])),
            "completed_tasks": _labels(context.get("completed", [])),
            "active_doing_tasks": _labels(context.get("doing", [])),
            "created_items": _labels(context.get("created", [])),
            "deferred_or_long_term": _labels(
                [
                    item for item in context.get("next_actions", [])
                    if isinstance(item, dict) and item.get("horizon") == "long_term"
                ]
            ),
            "thought_fragments": _labels(context.get("thoughts", [])),
            "sessions": _labels(context.get("sessions", [])),
            "blockers": context.get("blockers", []),
        }
        return json.dumps(payload, ensure_ascii=False, indent=2)

    # ----------------------------------------------------------------- public

    def classify(
        self,
        text: str,
        source: str = "unknown",
        context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = _fallback_classify(text)
        if not self.ready:
            return fallback
        try:
            raw = self._call("classifier", self._format_classifier_input(text))
            result = _parse_json(raw)
            self.last_error = ""  # success clears the sticky error marker
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"classifier: {exc}"
            log.warning("classifier call failed, using fallback: %s", exc)
            return fallback
        return _normalize_classification(result, text)

    def route(
        self,
        text: str,
        selected_item_ids: list[int] | None = None,
        selected_items: list[dict[str, Any]] | None = None,
        active_session: dict[str, Any] | None = None,
        ui_context: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        fallback = _fallback_route(text, selected_item_ids or [])
        if not self.ready:
            return fallback
        slim_items = digest_items(selected_items)
        try:
            raw = self._call(
                "command_router",
                self._format_router_input(text, slim_items, active_session, ui_context),
            )
            result = _parse_json(raw)
            self.last_error = ""
            return result
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"command_router: {exc}"
            log.warning("command_router call failed, using fallback: %s", exc)
            # Surface the real failure in user_feedback so the "Processed
            # locally" message makes its cause obvious in the GUI.
            enriched = dict(fallback)
            enriched["user_feedback"] = f"AI call failed — fallback used. {exc}"
            return enriched

    def structure_brain_dump(
        self,
        raw_text: str,
        active_tasks: list[dict[str, Any]] | None = None,
        long_term_items: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Return a JSON-ish dict with:
          items             — list of item dicts ready for ItemCreate
          structured_text   — Markdown for session display (may be empty)
          summary           — short one-liner
          used_fallback     — True when the LLM call failed or no API key

        The structurer prompt is expected to return JSON directly (see
        backend/prompts/task_structurer.md). We no longer parse Markdown
        server-side to recover items — the JSON path is the contract now.
        The Markdown parser stays in place only as a best-effort fallback
        for legacy outputs.
        """
        if not self.ready:
            return _fallback_structure(raw_text)
        try:
            raw = self._call(
                "task_structurer",
                self._format_structurer_input(raw_text, active_tasks or [], long_term_items or []),
            )
            data = _parse_json(raw)
            self.last_error = ""
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"task_structurer: {exc}"
            log.warning("task_structurer call failed, using fallback: %s", exc)
            return _fallback_structure(raw_text)

        items = data.get("items") or []
        summary = str(data.get("summary") or "")
        structured_markdown = str(data.get("structured_markdown") or "")
        if not structured_markdown:
            structured_markdown = _synthesize_markdown(items, summary)
        return {
            "items": items,
            "structured_text": structured_markdown,
            "summary": summary,
            "used_fallback": False,
        }

    def write_worklog(self, context: dict[str, Any]) -> dict[str, Any]:
        """worklog_writer contract: JSON by default. The LLM returns
        `{"content_md": "...", "summary": "..."}`. We pull content_md out
        for DB storage / export; the Markdown is a field, not the entire
        response body. Giving MD when requested = just using that field.
        """
        if not self.ready:
            return {"content_md": _fallback_worklog(context), "summary": "", "used_fallback": True}
        try:
            raw = self._call("worklog_writer", self._format_worklog_input(context))
            data = _parse_json(raw)
            content_md = str(data.get("content_md") or "").strip()
            if not content_md:
                raise ValueError("worklog_writer returned no content_md")
            self.last_error = ""
            return {
                "content_md": content_md,
                "summary": str(data.get("summary") or ""),
                "used_fallback": False,
            }
        except Exception as exc:  # noqa: BLE001
            self.last_error = f"worklog_writer: {exc}"
            log.warning("worklog_writer call failed, using fallback: %s", exc)
            return {"content_md": _fallback_worklog(context), "summary": "", "used_fallback": True}


# --------------------------------------------------------------------- helpers


def _labels(items: list[Any]) -> list[str]:
    labels: list[str] = []
    for item in items:
        if isinstance(item, dict):
            labels.append(str(item.get("title") or item.get("event_type") or item.get("id") or item))
        else:
            labels.append(str(item))
    return labels


def _parse_json(text: str) -> dict[str, Any]:
    return json.loads(_strip_code_fence(text))


def _strip_code_fence(text: str) -> str:
    stripped = text.strip()
    if stripped.startswith("```"):
        lines = stripped.splitlines()
        if lines:
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        stripped = "\n".join(lines).strip()
    if stripped.lower().startswith("json"):
        stripped = stripped[4:].strip()
    return stripped


def _normalize_classification(payload: dict[str, Any], original_text: str) -> dict[str, Any]:
    normalized = dict(payload)
    if "suggested_status" in normalized and "status" not in normalized:
        normalized["status"] = normalized["suggested_status"]
    if "suggested_horizon" in normalized and "horizon" not in normalized:
        normalized["horizon"] = normalized["suggested_horizon"]
    # `long_term` is a legitimate classifier output per prompt spec, but the
    # items table uses horizon for that dimension — convert here so the rest
    # of the pipeline sees a valid item_type.
    if normalized.get("item_type") == "long_term":
        normalized["item_type"] = "task"
        normalized["horizon"] = "long_term"
        normalized.setdefault("status", "todo")
    normalized.setdefault("title", original_text.strip().splitlines()[0][:120])
    normalized.setdefault("content", original_text.strip())
    normalized.setdefault("status", "inbox")
    normalized.setdefault("horizon", "now")
    normalized.setdefault("priority", 3)
    if not normalized.get("project"):
        normalized["project"] = ""
    normalized.setdefault("tags", [])
    if not isinstance(normalized.get("tags"), list):
        normalized["tags"] = []
    # Drives the router's "classifier as gate" decision. Coerce truthy/falsy
    # to a real bool so downstream .get() always yields a clean flag.
    normalized["decompose_as_brain_dump"] = bool(normalized.get("decompose_as_brain_dump"))
    return normalized


def _fallback_classify(text: str) -> dict[str, Any]:
    lower = text.lower()
    item_type = "task"
    horizon = "now"
    status = "todo"
    if any(token in lower for token in ["long-term", "long term", "장기", "나중에"]):
        horizon = "long_term"
    if any(token in lower for token in ["생각", "idea", "아이디어", "thought"]):
        item_type = "thought"
        status = "inbox"
    if any(token in lower for token in ["업무일지", "worklog", "journal"]):
        item_type = "journal_seed"
        status = "inbox"
    if any(token in lower for token in ["note", "메모"]):
        item_type = "note"
        status = "inbox"
    return {
        "item_type": item_type,
        "title": text.strip().splitlines()[0][:120],
        "content": text.strip(),
        "status": status,
        "horizon": horizon,
        "priority": 3,
        "project": "",
        "tags": [],
        "confidence": 0.55,
        # Deterministic fallback has no LLM to judge multiplicity — default
        # to single-item. If the user lands in offline mode and wants
        # decomposition they can still hit the explicit Structure button in
        # the Sessions tab.
        "decompose_as_brain_dump": False,
    }


def _fallback_route(text: str, selected_item_ids: list[int]) -> dict[str, Any]:
    lower = text.lower()
    actions: list[dict[str, Any]] = []

    # worklog
    if "업무일지" in text or "worklog" in lower:
        actions.append({"type": "generate_worklog", "scope": "today"})

    # state transitions on selected
    if selected_item_ids and any(tok in lower or tok in text for tok in ["doing", "진행", "착수"]):
        for item_id in selected_item_ids:
            actions.append({"type": "mark_selected_item_doing", "item_id": item_id})
    if selected_item_ids and any(tok in lower or tok in text for tok in ["done", "완료", "끝냈"]):
        for item_id in selected_item_ids:
            actions.append({"type": "mark_selected_item_done", "item_id": item_id})

    # priority change on selected — check BEFORE the generic capture bucket
    priority = _detect_priority(text)
    if selected_item_ids and priority is not None and _has_priority_keyword(text):
        for item_id in selected_item_ids:
            actions.append({
                "type": "reprioritize_items",
                "item_id": item_id,
                "payload": {"priority": priority},
            })

    # hard delete (user must say "완전히 / 영구 / permanently"); checked
    # BEFORE the soft-archive branch so the more explicit intent wins.
    hard_delete = selected_item_ids and any(
        tok in text or tok in lower
        for tok in ["완전히 지워", "완전 삭제", "영구", "영영", "permanently", "hard delete"]
    )
    if hard_delete:
        for item_id in selected_item_ids:
            actions.append({"type": "delete_selected_item", "item_id": item_id})
    elif selected_item_ids and any(tok in text or tok in lower for tok in ["지워", "삭제", "없애", "archive", "delete"]):
        for item_id in selected_item_ids:
            actions.append({"type": "archive_selected_item", "item_id": item_id})

    # long-term (explicit new capture)
    if not selected_item_ids and ("장기" in text or "long-term" in lower or "long term" in lower):
        actions.append({"type": "create_item", "item_kind": "long_term", "content": text})

    # Fallback of last resort — only capture when no other action matched
    # AND nothing is selected. With a selection present we prefer no_op so
    # we don't accidentally create a new item out of a failed command.
    if not actions:
        if selected_item_ids:
            actions.append({"type": "no_op"})
        else:
            actions.append({"type": "create_item", "content": text})

    # Mode classification by action role rather than action count: multiple
    # reprioritize actions on the same input are still one *command*, not a
    # hybrid. Hybrid is reserved for genuinely mixed intents (e.g. capture
    # + generate_worklog in one utterance).
    capture_types = {"create_item", "classify_and_store"}
    has_capture = any(a["type"] in capture_types for a in actions)
    has_command = any(a["type"] not in capture_types for a in actions)
    if has_capture and has_command:
        mode = "hybrid"
    elif has_command:
        mode = "command"
    else:
        mode = "content_capture"
    return {"mode": mode, "actions": actions, "user_feedback": "Processed locally."}


# --- helpers for priority detection --------------------------------------

_PRIORITY_KEYWORDS = ("중요도", "우선순위", "우선도", "priority", "importance")

_PRIORITY_LEVELS: tuple[tuple[tuple[str, ...], int], ...] = (
    (("매우 높음", "매우높음", "최우선", "긴급", "critical"), 1),
    (("높음", "high"), 2),
    (("보통", "중간", "기본", "normal"), 3),
    (("낮음", "low"), 4),
    (("매우 낮음", "매우낮음", "someday", "나중에", "언젠가"), 5),
)


def _has_priority_keyword(text: str) -> bool:
    lower = text.lower()
    return any(tok in lower or tok in text for tok in _PRIORITY_KEYWORDS)


def _detect_priority(text: str) -> int | None:
    """Map a natural-language priority phrase to 1..5, or return None."""
    lower = text.lower()
    for terms, level in _PRIORITY_LEVELS:
        if any(term in lower or term in text for term in terms):
            return level
    # Also pick up explicit "P1" / "P3" / bare "1".."5" (only if priority
    # keyword present — checked by caller).
    import re
    match = re.search(r"[pP]([1-5])\b", text)
    if match:
        return int(match.group(1))
    match = re.search(r"\b([1-5])\b", text)
    if match:
        return int(match.group(1))
    return None


def _fallback_structure(raw_text: str) -> dict[str, Any]:
    """Offline fallback — no LLM, deterministic: one item per non-empty line,
    all typed as a todo task on the `now` horizon. Better than no output at
    all, and the user can edit in the GUI afterwards."""
    lines = [line.strip(" -\t") for line in raw_text.splitlines() if line.strip()]
    items: list[dict[str, Any]] = [
        {
            "item_type": "task",
            "title": line[:120],
            "content": line,
            "status": "todo",
            "horizon": "now",
            "priority": 3,
            "project": "",
            "tags": [],
        }
        for line in lines
    ]
    summary = lines[0] if lines else ""
    return {
        "items": items,
        "structured_text": _synthesize_markdown(items, summary),
        "summary": summary,
        "used_fallback": True,
    }


def _synthesize_markdown(items: list[dict[str, Any]], summary: str) -> str:
    """Render parsed items into a readable Markdown block for the session's
    structured_text column. Only used when the LLM didn't supply its own
    `structured_markdown` field."""
    if not items:
        return f"## Summary\n\n{summary or '(empty brain dump)'}\n"
    now = [i for i in items if i.get("horizon") != "long_term" and i.get("item_type") == "task"]
    longterm = [i for i in items if i.get("horizon") == "long_term"]
    thoughts = [i for i in items if i.get("item_type") in {"thought", "note", "journal_seed"}]

    def _block(title: str, rows: list[dict[str, Any]]) -> str:
        if not rows:
            return f"### {title}\n- (없음)\n"
        lines = [f"### {title}"]
        for item in rows:
            pri = item.get("priority")
            prefix = f"[P{pri}] " if isinstance(pri, int) and 1 <= pri <= 5 else ""
            lines.append(f"- {prefix}{item.get('title', '')}")
        return "\n".join(lines) + "\n"

    summary_block = f"## Summary\n\n{summary}\n" if summary else ""
    return (
        summary_block
        + _block("지금 할 일 (Active Now)", now)
        + "\n"
        + _block("장기 보존 (Long-term Backlog)", longterm)
        + "\n"
        + _block("생각 / 메모 (Thought Fragments)", thoughts)
    )


def _fallback_worklog(context: dict[str, Any]) -> str:
    date = context.get("log_date", local_date())
    sections = [
        ("오늘 착수한 작업 (Started)",   context.get("started", [])),
        ("진행 중인 작업 (In Progress)", context.get("doing", [])),
        ("완료한 작업 (Completed)",      context.get("completed", [])),
        ("보류 / 장기 (Deferred)",       [i for i in context.get("next_actions", []) if isinstance(i, dict) and i.get("horizon") == "long_term"]),
        ("메모 및 관찰 (Notes)",         context.get("thoughts", [])),
        ("이슈 / 블로커 (Blockers)",     context.get("blockers", [])),
    ]
    lines = [f"# 📅 업무일지 — {date}", ""]
    for idx, (title, items) in enumerate(sections, 1):
        lines.extend([f"## {idx}. {title}", ""])
        if not items:
            lines.append("- (없음)")
        else:
            for item in items:
                label = item.get("title") if isinstance(item, dict) else str(item)
                lines.append(f"- {label}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
