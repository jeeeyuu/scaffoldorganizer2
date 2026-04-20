from __future__ import annotations

import logging
import threading
import time
from typing import Any

import requests

from backend.config import AppConfig
from backend.db.database import connect
from backend.models.schemas import ItemCreate
from backend.services.ai_service import AIService, _fallback_classify
from backend.services.event_service import record_event
from backend.services.item_service import create_item
from backend.utils.time import utc_now

log = logging.getLogger(__name__)

# Skip LLM classification for trivially short captures — the fallback already
# handles single-line tasks and thoughts well, and every API call costs.
_SKIP_LLM_LEN = 40
_BACKOFF_CAP_SECONDS = 300.0


class TelegramPollingService:
    """Polling worker that lives only while the backend process is running."""

    def __init__(self, config: AppConfig, ai: AIService) -> None:
        self.config = config
        self.ai = ai
        self._running = False
        self._thread: threading.Thread | None = None
        self.last_error = ""

    @property
    def active(self) -> bool:
        return bool(self._running and self._thread and self._thread.is_alive())

    def start(self) -> None:
        if not self.config.telegram_enabled:
            return
        if self.active:
            return
        allowed = self.config.telegram_allowed_chat_ids
        if allowed:
            log.info("telegram polling start — allowlist has %d chat(s): %s", len(allowed), allowed)
        else:
            log.warning(
                "telegram polling start — telegram_allowed_chat_ids is EMPTY, "
                "all incoming chats will be accepted. Populate the list in config.json to restrict."
            )
        self._running = True
        self._thread = threading.Thread(target=self._poll, name="telegram-polling", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.config.telegram_bot_token}/{method}"

    def _load_offset(self) -> int:
        with connect(self.config) as conn:
            row = conn.execute("SELECT offset FROM telegram_offsets WHERE bot_key = 'default'").fetchone()
            return int(row["offset"]) if row else 0

    def _save_offset(self, offset: int) -> None:
        with connect(self.config) as conn:
            conn.execute(
                """
                INSERT INTO telegram_offsets (bot_key, offset, updated_at)
                VALUES ('default', ?, ?)
                ON CONFLICT(bot_key) DO UPDATE SET offset = excluded.offset, updated_at = excluded.updated_at
                """,
                (offset, utc_now()),
            )

    def _poll(self) -> None:
        offset = self._load_offset()
        backoff = self.config.telegram_poll_interval_seconds
        while self._running:
            try:
                response = requests.get(
                    self._url("getUpdates"),
                    params={"offset": offset, "timeout": 25},
                    timeout=35,
                )
                payload = response.json()
                if payload.get("ok"):
                    for update in payload.get("result", []):
                        offset = int(update["update_id"]) + 1
                        self._handle_update(update)
                    self._save_offset(offset)
                    backoff = self.config.telegram_poll_interval_seconds
                else:
                    raise RuntimeError(f"Telegram API not ok: {payload.get('description')}")
            except Exception as exc:  # noqa: BLE001
                self.last_error = str(exc)
                log.warning("Telegram polling error (sleeping %.1fs): %s", backoff, exc)
                time.sleep(backoff)
                backoff = min(backoff * 2, _BACKOFF_CAP_SECONDS)

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id_int = int(chat_id)

        # Authorization: drop anything not on the allowlist BEFORE any AI
        # call or DB write. This is the single choke point for incoming
        # Telegram traffic — keep it at the top of the handler and leave a
        # log trail so the user can audit dropped messages and discover the
        # chat_id they need to add.
        allowed = self.config.telegram_allowed_chat_ids
        if allowed and chat_id_int not in allowed:
            log.warning(
                "telegram: dropped message from unauthorized chat_id=%s (title=%r, username=%r) — add to telegram_allowed_chat_ids to accept",
                chat_id_int,
                chat.get("title"),
                chat.get("username") or chat.get("first_name"),
            )
            return

        text = str(message.get("text") or "").strip()
        if not text:
            return

        if len(text) <= _SKIP_LLM_LEN and "\n" not in text:
            classification = _fallback_classify(text)
        else:
            classification = self.ai.classify(text)
        with connect(self.config) as conn:
            item = create_item(
                conn,
                ItemCreate(
                    item_type=classification.get("item_type", "task"),
                    title=classification.get("title") or text[:120],
                    content=text,
                    status=classification.get("status", "inbox"),
                    horizon=classification.get("horizon", "now"),
                    priority=int(classification.get("priority", 3)),
                    source="telegram",
                    project=classification.get("project", ""),
                    tags=classification.get("tags", []),
                    external_ref=str(update.get("update_id")),
                ),
            )
            record_event(conn, "telegram_received", item_id=item["id"], payload={"chat_id": chat_id_int})

