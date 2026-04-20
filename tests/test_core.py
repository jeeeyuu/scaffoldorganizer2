from __future__ import annotations

import sqlite3
import sys
import types
from pathlib import Path

from backend.config import AIRoleConfig, AppConfig
from backend.db.database import init_db
from backend.models.schemas import ItemCreate
from backend.services.ai_service import AIService, digest_items
from backend.services.brain_dump_parser import parse_brain_dump_markdown
from backend.services.item_service import change_status, create_item, delete_items, reset_all_items
from backend.services.worklog_service import assemble_worklog_context


def _config_with_roles(**overrides) -> AppConfig:
    roles = {
        name: AIRoleConfig(
            prompt_id="",
            prompt_file=f"backend/prompts/{name}.md",
            response_format="json" if name in {"classifier", "command_router"} else "markdown",
        )
        for name in ("command_router", "classifier", "task_structurer", "worklog_writer")
    }
    data = {"ai_roles": roles}
    data.update(overrides)
    return AppConfig(**data)


def test_config_defaults_are_valid() -> None:
    config = _config_with_roles(db_path="data/test.sqlite3")
    assert config.backend_url == "http://127.0.0.1:8765"
    assert config.role("classifier").response_format == "json"


def test_db_bootstrap_and_state_transition(tmp_path: Path) -> None:
    config = _config_with_roles(db_path=str(tmp_path / "app.sqlite3"))
    db_path = init_db(config)
    assert db_path.exists()
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    item = create_item(conn, ItemCreate(title="Write integration test", source="manual"))
    changed = change_status(conn, item["id"], "doing")
    assert changed
    assert changed["status"] == "doing"
    conn.close()


def test_router_fallback_creates_actions() -> None:
    ai = AIService(_config_with_roles())
    routed = ai.route("오늘 업무일지 만들어줘", [])
    assert routed["actions"][0]["type"] == "generate_worklog"


def test_worklog_context(tmp_path: Path) -> None:
    config = _config_with_roles(db_path=str(tmp_path / "app.sqlite3"))
    db_path = init_db(config)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    item = create_item(conn, ItemCreate(title="Finish feature", source="manual"))
    change_status(conn, item["id"], "done")
    context = assemble_worklog_context(conn)
    assert context["completed"]
    conn.close()


def test_ai_service_sends_developer_and_user_messages(monkeypatch) -> None:
    """When no prompt_id is configured, the local .md prompt is sent as a
    developer message alongside the user payload."""

    captured: dict = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return types.SimpleNamespace(output_text='{"item_type": "task", "title": "t"}')

    class FakeOpenAI:
        def __init__(self, api_key: str) -> None:
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

    ai = AIService(_config_with_roles(openai_api_key="test-key"))
    ai.classify("raw text should be wrapped")

    kwargs = captured["kwargs"]
    # Only prompt-id path omits model; the fallback path must still supply it.
    assert kwargs["model"]  # exact value doesn't matter, just that it's set
    assert "temperature" not in kwargs  # stripped — dashboard owns this
    assert "max_output_tokens" not in kwargs  # same
    assert "prompt" not in kwargs  # no prompt_id configured → developer path
    assert kwargs["input"][0]["role"] == "developer"
    assert kwargs["input"][1]["role"] == "user"
    assert "[INPUT]" in kwargs["input"][1]["content"]
    assert "raw text should be wrapped" in kwargs["input"][1]["content"]


def test_ai_service_uses_prompt_id_when_set(monkeypatch) -> None:
    """When prompt_id is configured, the developer message is omitted and the
    server-side prompt is referenced instead."""

    captured: dict = {}

    class FakeResponses:
        def create(self, **kwargs):
            captured["kwargs"] = kwargs
            return types.SimpleNamespace(output_text='{"mode": "command", "actions": [], "user_feedback": ""}')

    class FakeOpenAI:
        def __init__(self, api_key: str) -> None:
            self.responses = FakeResponses()

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))

    config = _config_with_roles(openai_api_key="test-key")
    config.ai_roles["command_router"].prompt_id = "pmpt_xyz"
    ai = AIService(config)
    ai.route("hello", [])

    kwargs = captured["kwargs"]
    assert kwargs["prompt"] == {"id": "pmpt_xyz"}
    assert "model" not in kwargs  # dashboard owns model selection
    assert "temperature" not in kwargs
    assert "max_output_tokens" not in kwargs
    assert len(kwargs["input"]) == 1
    assert kwargs["input"][0]["role"] == "user"
    assert "[CONTEXT]" in kwargs["input"][0]["content"]
    assert "[USER INPUT]" in kwargs["input"][0]["content"]


def test_brain_dump_parser_splits_sections() -> None:
    md = """## 🍎 브레인덤프 분류 및 구조화
(summary)

## 🥑 우선순위 조정 & 실행 원자화

### 지금 할 일 (Active Now)
| 우선순위 | 작업 | 예상시간 |
|---|---|---|
| P1 | 데이터 컬럼 정의 확정 | 30m |
| P2 | preprocessing pipeline 설계 | 1h |

### 장기 보존 (Long-term Backlog)
- long-read RNA foundation model 아이디어
- alignment benchmark 재정의

### 생각 / 메모 (Thought Fragments)
- ibus 자동 기동 이슈
- Noto Sans fallback 검증
"""
    items = parse_brain_dump_markdown(md)
    types = [(i["item_type"], i["horizon"], i["title"]) for i in items]
    # table rows (Active Now) → task/now, bullets preserve order
    assert ("task", "now", "데이터 컬럼 정의 확정") in types
    assert ("task", "now", "preprocessing pipeline 설계") in types
    assert ("task", "long_term", "long-read RNA foundation model 아이디어") in types
    assert ("thought", "now", "ibus 자동 기동 이슈") in types
    assert len(items) == 6

    # Active Now table carries priorities (P1, P2) and the parser must
    # lift those into the item dicts; long-term / thought sections don't.
    by_title = {i["title"]: i for i in items}
    assert by_title["데이터 컬럼 정의 확정"].get("priority") == 1
    assert by_title["preprocessing pipeline 설계"].get("priority") == 2
    assert "priority" not in by_title["ibus 자동 기동 이슈"]


def test_reset_all_items_wipes_and_nullifies_events(tmp_path: Path) -> None:
    config = _config_with_roles(db_path=str(tmp_path / "app.sqlite3"))
    init_db(config)
    conn = sqlite3.connect(init_db(config))
    conn.row_factory = sqlite3.Row
    create_item(conn, ItemCreate(title="a", source="manual"))
    create_item(conn, ItemCreate(title="b", source="manual", horizon="long_term"))
    deleted = reset_all_items(conn)
    assert deleted == 2
    remaining_items = conn.execute("SELECT COUNT(*) FROM items").fetchone()[0]
    assert remaining_items == 0
    # Events preserved but item_id nulled so the audit trail survives.
    rows = conn.execute("SELECT item_id FROM events WHERE event_type = 'created'").fetchall()
    assert rows
    assert all(r["item_id"] is None for r in rows)
    conn.close()


def test_classifier_normalizes_decompose_flag() -> None:
    """normalize_classification must coerce the decompose field into a
    real bool and default it to False when missing."""
    from backend.services.ai_service import _normalize_classification

    # Missing entirely → False
    out = _normalize_classification({"item_type": "task", "title": "t"}, "t")
    assert out["decompose_as_brain_dump"] is False

    # Truthy string → True
    out = _normalize_classification({"decompose_as_brain_dump": "true"}, "x")
    assert out["decompose_as_brain_dump"] is True

    # Explicit False passes through
    out = _normalize_classification({"decompose_as_brain_dump": False}, "x")
    assert out["decompose_as_brain_dump"] is False


def test_fallback_router_recognises_priority_command() -> None:
    """Without an API key the local fallback must still turn a Korean
    priority command against selected items into reprioritize_items
    actions — not a create_item capture."""
    ai = AIService(_config_with_roles())
    routed = ai.route("선택한 2개 중요도 매우높음으로 올려줘", [201, 202])
    types = [a["type"] for a in routed["actions"]]
    assert types.count("reprioritize_items") == 2
    assert all(a["payload"]["priority"] == 1 for a in routed["actions"] if a["type"] == "reprioritize_items")
    assert routed["mode"] == "command"


def test_fallback_router_hard_vs_soft_delete() -> None:
    ai = AIService(_config_with_roles())
    soft = ai.route("선택한거 지워줘", [99])
    assert soft["actions"][0]["type"] == "archive_selected_item"
    hard = ai.route("선택한거 완전히 지워줘", [99])
    assert hard["actions"][0]["type"] == "delete_selected_item"
    permanent = ai.route("영구 삭제", [99])
    assert permanent["actions"][0]["type"] == "delete_selected_item"


def test_delete_items_removes_rows_and_nulls_events(tmp_path: Path) -> None:
    config = _config_with_roles(db_path=str(tmp_path / "app.sqlite3"))
    init_db(config)
    conn = sqlite3.connect(init_db(config))
    conn.row_factory = sqlite3.Row
    a = create_item(conn, ItemCreate(title="a", source="manual"))
    b = create_item(conn, ItemCreate(title="b", source="manual"))
    deleted = delete_items(conn, [a["id"], b["id"]])
    assert deleted == 2
    assert conn.execute("SELECT COUNT(*) FROM items").fetchone()[0] == 0
    rows = conn.execute("SELECT item_id FROM events WHERE event_type = 'created'").fetchall()
    assert rows and all(r["item_id"] is None for r in rows)
    conn.close()


def test_digest_items_strips_heavy_fields() -> None:
    source = [{
        "id": 1,
        "title": "t",
        "content": "x" * 500,
        "tags": ["a", "b"],
        "horizon": "now",
        "status": "todo",
        "priority": 2,
        "project": "",
        "created_at": "2026-04-20T00:00:00Z",
    }]
    slim = digest_items(source)
    assert slim == [{"id": 1, "title": "t", "horizon": "now", "status": "todo", "priority": 2}]
