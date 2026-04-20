from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field


ItemType = Literal["task", "thought", "journal_seed", "note"]
ItemStatus = Literal["inbox", "todo", "doing", "done", "archived"]
Horizon = Literal["now", "soon", "later", "long_term"]
Source = Literal["telegram", "chat_input", "brain_dump", "manual", "system"]


class ItemCreate(BaseModel):
    item_type: ItemType = "task"
    title: str = Field(min_length=1)
    content: str = ""
    status: ItemStatus = "inbox"
    horizon: Horizon = "now"
    priority: int = Field(default=3, ge=1, le=5)
    source: Source = "manual"
    project: str = ""
    tags: list[str] = Field(default_factory=list)
    scheduled_date: str | None = None
    due_date: str | None = None
    session_id: int | None = None
    external_ref: str | None = None


class ItemUpdate(BaseModel):
    item_type: ItemType | None = None
    title: str | None = None
    content: str | None = None
    status: ItemStatus | None = None
    horizon: Horizon | None = None
    priority: int | None = Field(default=None, ge=1, le=5)
    project: str | None = None
    tags: list[str] | None = None
    scheduled_date: str | None = None
    due_date: str | None = None


class StatusChange(BaseModel):
    status: ItemStatus


class SessionSave(BaseModel):
    title: str = "Untitled session"
    raw_text: str = ""
    structured_text: str = ""


class UIContext(BaseModel):
    active_tab: str | None = None
    filters: dict[str, Any] = Field(default_factory=dict)


class ChatCommand(BaseModel):
    text: str = Field(min_length=1)
    selected_item_ids: list[int] = Field(default_factory=list)
    selected_items: list[dict[str, Any]] = Field(default_factory=list)
    active_session: dict[str, Any] | None = None
    ui_context: UIContext = Field(default_factory=UIContext)


class BrainDumpRequest(BaseModel):
    title: str = "Brain dump"
    raw_text: str = Field(min_length=1)


class WorklogGenerateRequest(BaseModel):
    log_date: str | None = None


class WorklogSaveRequest(BaseModel):
    log_date: str
    title: str = "Work Log"
    content_md: str
    context_summary: dict[str, Any] = Field(default_factory=dict)


class RouterAction(BaseModel):
    type: str
    item_kind: str | None = None
    content: str | None = None
    item_id: int | None = None
    status: ItemStatus | None = None
    scope: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)


class RouterResult(BaseModel):
    mode: Literal["command", "content_capture", "capture", "hybrid"] = "content_capture"
    actions: list[RouterAction] = Field(default_factory=list)
    user_feedback: str = ""
