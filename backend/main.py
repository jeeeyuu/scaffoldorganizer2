from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Response
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel, Field

from backend.config import AppConfig, load_config
from backend.db.database import connect, init_db
from backend.models.schemas import (
    BrainDumpRequest,
    ChatCommand,
    ItemCreate,
    ItemUpdate,
    SessionSave,
    StatusChange,
    WorklogGenerateRequest,
    WorklogSaveRequest,
)
from backend.services.ai_service import AIService
from backend.services.brain_dump_service import structure_and_create
from backend.services.export_service import export_items_markdown
from backend.services.item_service import archive_item, change_status, create_item, delete_items, get_item, list_items, reset_all_items, update_item
from backend.services.router_service import handle_chat_command
from backend.services.session_service import export_session_markdown, get_session, list_sessions, save_session
from backend.services.telegram_service import TelegramPollingService
from backend.services.worklog_service import export_worklog, generate_worklog_draft, list_worklogs, save_worklog_draft

log = logging.getLogger(__name__)


class AppRuntime:
    def __init__(self) -> None:
        self.config: AppConfig | None = None
        self.ai: AIService | None = None
        self.telegram: TelegramPollingService | None = None
        self.shutdown_requested = False


runtime = AppRuntime()


@asynccontextmanager
async def lifespan(app: FastAPI):
    config = load_config()
    init_db(config)
    runtime.config = config
    runtime.ai = AIService(config)
    runtime.telegram = TelegramPollingService(config, runtime.ai)
    runtime.telegram.start()
    yield
    if runtime.telegram:
        runtime.telegram.stop()


app = FastAPI(title="ScaffoldOrganizer 2.0 Backend", lifespan=lifespan)
# The GUI loads from file:// (PyWebView) or http://127.0.0.1, never from a
# remote origin. Keeping CORS tight here also blunts the risk of /shutdown
# being hit by an accidentally-open browser tab on the same machine.
app.add_middleware(
    CORSMiddleware,
    allow_origin_regex=r"^(null|file://.*|http://(localhost|127\.0\.0\.1)(:\d+)?)$",
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def cfg() -> AppConfig:
    if runtime.config is None:
        raise HTTPException(status_code=503, detail="Backend is not initialized")
    return runtime.config


def ai() -> AIService:
    if runtime.ai is None:
        raise HTTPException(status_code=503, detail="AI service is not initialized")
    return runtime.ai


@app.get("/health")
def health() -> dict:
    return {"ok": True}


@app.get("/config/ui")
def ui_config() -> dict:
    """Minimal, non-secret config values the GUI needs at startup."""
    config = cfg()
    return {
        "app_name": config.app_name,
        "ui_refresh_interval_ms": config.ui_refresh_interval_ms,
    }


@app.get("/status")
def status() -> dict:
    telegram = runtime.telegram
    ai_service = runtime.ai

    # AI dot semantics: "Ready" only when an API key is set AND no recent
    # call failed. "Error" means key is set but the last request blew up
    # (so the user sees the sticky fallback and can read the cause via
    # ai_error). "Local fallback" means no key configured.
    if not ai_service:
        ai_state = "Not initialized"
    elif ai_service.ready and ai_service.last_error:
        ai_state = "Error"
    elif ai_service.ready:
        ai_state = "Ready"
    else:
        ai_state = "Local fallback"

    return {
        "backend": "Active",
        "telegram": "Active" if telegram and telegram.active else ("Idle" if cfg().telegram_enabled else "Disabled"),
        "telegram_error": telegram.last_error if telegram else "",
        "ai": ai_state,
        "ai_error": ai_service.last_error if ai_service else "",
        "shutdown_requested": runtime.shutdown_requested,
    }


@app.post("/shutdown")
async def shutdown() -> dict:
    runtime.shutdown_requested = True
    if runtime.telegram:
        runtime.telegram.stop()

    async def delayed_exit() -> None:
        await asyncio.sleep(0.2)
        import os
        import signal

        os.kill(os.getpid(), signal.SIGTERM)

    asyncio.create_task(delayed_exit())
    return {"ok": True}


@app.get("/items")
def api_list_items(
    status: str | None = None,
    horizon: str | None = None,
    item_type: str | None = None,
    exclude_horizon: str | None = None,
) -> list[dict]:
    with connect(cfg()) as conn:
        return list_items(
            conn,
            status=status,
            horizon=horizon,
            item_type=item_type,
            exclude_horizon=exclude_horizon,
        )


@app.post("/items")
def api_create_item(payload: ItemCreate) -> dict:
    with connect(cfg()) as conn:
        return create_item(conn, payload)


@app.get("/items/{item_id}")
def api_get_item(item_id: int) -> dict:
    with connect(cfg()) as conn:
        item = get_item(conn, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return item


@app.patch("/items/{item_id}")
def api_update_item(item_id: int, payload: ItemUpdate) -> dict:
    with connect(cfg()) as conn:
        item = update_item(conn, item_id, payload)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return item


@app.post("/items/{item_id}/status")
def api_change_status(item_id: int, payload: StatusChange) -> dict:
    with connect(cfg()) as conn:
        item = change_status(conn, item_id, payload.status)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return item


@app.post("/items/{item_id}/archive")
def api_archive_item(item_id: int) -> dict:
    with connect(cfg()) as conn:
        item = archive_item(conn, item_id)
        if item is None:
            raise HTTPException(status_code=404, detail="Item not found")
        return item


@app.post("/items/reset")
def api_reset_items() -> dict:
    """Destructive: delete ALL items (long-term included). The GUI's Reset
    button hits this after confirming with the user."""
    with connect(cfg()) as conn:
        deleted = reset_all_items(conn)
    return {"deleted": deleted}


@app.delete("/items/{item_id}")
def api_delete_item(item_id: int) -> dict:
    """Hard-delete a single item row. Distinct from /items/{id}/archive
    (status=archived, reversible)."""
    with connect(cfg()) as conn:
        deleted = delete_items(conn, [item_id])
    if deleted == 0:
        raise HTTPException(status_code=404, detail="Item not found")
    return {"deleted": deleted}


class BulkDeleteRequest(BaseModel):
    item_ids: list[int] = Field(default_factory=list)


@app.post("/items/delete")
def api_bulk_delete_items(payload: BulkDeleteRequest) -> dict:
    """Hard-delete multiple item rows in one transaction."""
    with connect(cfg()) as conn:
        deleted = delete_items(conn, payload.item_ids)
    return {"deleted": deleted}


@app.post("/chat/command")
def api_chat_command(payload: ChatCommand) -> dict:
    # DB connection is released during the LLM call inside handle_chat_command.
    return handle_chat_command(cfg(), ai(), payload)


@app.post("/chat/capture")
def api_chat_capture(payload: ChatCommand) -> dict:
    classification = ai().classify(payload.text)
    with connect(cfg()) as conn:
        return create_item(
            conn,
            ItemCreate(
                item_type=classification.get("item_type", "task"),
                title=classification.get("title") or payload.text[:120],
                content=payload.text,
                status=classification.get("status", "inbox"),
                horizon=classification.get("horizon", "now"),
                priority=int(classification.get("priority", 3)),
                source="chat_input",
                project=classification.get("project", ""),
                tags=classification.get("tags", []),
            ),
        )


@app.post("/brain-dump/structure")
def api_structure_brain_dump(payload: BrainDumpRequest) -> dict:
    return structure_and_create(
        cfg(),
        ai(),
        raw_text=payload.raw_text,
        title=payload.title,
        source="brain_dump",
    )


@app.get("/sessions")
def api_list_sessions() -> list[dict]:
    with connect(cfg()) as conn:
        return list_sessions(conn)


@app.post("/sessions/save")
def api_save_session(payload: SessionSave) -> dict:
    with connect(cfg()) as conn:
        return save_session(conn, payload)


@app.get("/sessions/{session_id}")
def api_get_session(session_id: int) -> dict:
    with connect(cfg()) as conn:
        session = get_session(conn, session_id)
        if session is None:
            raise HTTPException(status_code=404, detail="Session not found")
        return session


@app.post("/export/markdown")
def api_export_markdown() -> dict:
    with connect(cfg()) as conn:
        path = export_items_markdown(conn, cfg())
        return {"path": str(path)}


@app.post("/sessions/{session_id}/export")
def api_export_session(session_id: int) -> dict:
    with connect(cfg()) as conn:
        path = export_session_markdown(conn, cfg(), session_id)
        return {"path": str(path)}


@app.post("/worklog/generate")
def api_generate_worklog(payload: WorklogGenerateRequest) -> dict:
    # Draft only — the GUI shows this to the user before persisting so
    # regeneration doesn't leave orphan rows in the worklogs table.
    return generate_worklog_draft(cfg(), ai(), log_date=payload.log_date)


@app.post("/worklog/save")
def api_save_worklog(payload: WorklogSaveRequest) -> dict:
    return save_worklog_draft(
        cfg(),
        log_date=payload.log_date,
        title=payload.title,
        content_md=payload.content_md,
        context_summary=payload.context_summary,
    )


@app.get("/worklogs")
def api_list_worklogs() -> list[dict]:
    with connect(cfg()) as conn:
        return list_worklogs(conn)


@app.post("/worklogs/{worklog_id}/export")
def api_export_worklog(worklog_id: int) -> dict:
    with connect(cfg()) as conn:
        path = export_worklog(conn, cfg(), worklog_id)
        return {"path": str(path)}


@app.get("/")
def root() -> Response:
    return Response("ScaffoldOrganizer 2.0 backend is running.", media_type="text/plain")
