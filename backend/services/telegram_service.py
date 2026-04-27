from __future__ import annotations

import html
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

# Commands registered with BotFather's setMyCommands so Telegram clients
# show them as autocomplete choices when the user types "/".
_BOT_COMMANDS = [
    {"command": "start",  "description": "봇 소개"},
    {"command": "help",   "description": "사용법 / 명령어 목록"},
    {"command": "stats",  "description": "Inbox / Active / Doing 현재 개수"},
    {"command": "list",   "description": "최근 Inbox 5개"},
    {"command": "whoami", "description": "내 chat_id 보기 (allowlist 추가용)"},
]

_HELP_TEXT = (
    "이 봇에 그냥 메시지를 보내면 앱 Inbox 에 자동 캡처됩니다. "
    "긴 브레인덤프는 여러 item 으로 분해됩니다.\n\n"
    "<b>명령어</b>\n"
    "/help   — 이 도움말\n"
    "/stats  — Inbox / Active / Doing 개수\n"
    "/list   — 최근 Inbox 5개\n"
    "/whoami — 내 chat_id (allowlist 설정용)"
)


class TelegramPollingService:
    """Polling worker that lives only while the backend process is running."""

    def __init__(self, config: AppConfig, ai: AIService) -> None:
        self.config = config
        self.ai = ai
        self._running = False
        self._thread: threading.Thread | None = None
        self.last_error = ""
        self._commands_registered = False

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
        self._register_bot_commands()
        self._running = True
        self._thread = threading.Thread(target=self._poll, name="telegram-polling", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._running = False
        if self._thread:
            self._thread.join(timeout=5)

    # ---------------------------------------------------------------- HTTP

    def _url(self, method: str) -> str:
        return f"https://api.telegram.org/bot{self.config.telegram_bot_token}/{method}"

    def _send_message(self, chat_id: int, text: str, *, parse_mode: str | None = "HTML") -> None:
        """Send a reply. Failures are logged but don't stop polling — the
        user still gets their item captured, they just miss the ack."""
        try:
            payload: dict[str, Any] = {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            }
            if parse_mode:
                payload["parse_mode"] = parse_mode
            resp = requests.post(self._url("sendMessage"), json=payload, timeout=10)
            if not resp.ok:
                log.warning("sendMessage %d failed: %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("sendMessage exception: %s", exc)

    def _register_bot_commands(self) -> None:
        """Register the / command autocomplete list with Telegram.

        Clients only refresh this list occasionally, so changes can take a
        minute to propagate on the user side. We call it once per process
        start; failures are non-fatal (the bot still works without
        autocomplete)."""
        try:
            resp = requests.post(
                self._url("setMyCommands"),
                json={"commands": _BOT_COMMANDS},
                timeout=5,
            )
            if resp.ok:
                self._commands_registered = True
                log.info("telegram: registered %d bot commands", len(_BOT_COMMANDS))
            else:
                log.warning("setMyCommands %d: %s", resp.status_code, resp.text[:200])
        except Exception as exc:  # noqa: BLE001
            log.warning("setMyCommands exception: %s", exc)

    # -------------------------------------------------------------- polling

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

    # ---------------------------------------------------------- update flow

    def _handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message") or {}
        chat = message.get("chat", {})
        chat_id = chat.get("id")
        if chat_id is None:
            return
        chat_id_int = int(chat_id)
        text = str(message.get("text") or "").strip()
        if not text:
            return

        is_command = text.startswith("/")
        cmd = text.split()[0].split("@")[0].lower() if is_command else ""

        # /whoami bypasses the allowlist — its whole purpose is to let the
        # user discover the chat_id they need to add to the allowlist in
        # the first place. Every other command still requires authorization.
        if cmd == "/whoami":
            self._send_message(
                chat_id_int,
                f"Chat ID: <code>{chat_id_int}</code>\n"
                f"allowlist 에 추가: <code>[{chat_id_int}]</code>",
            )
            return

        # Authorization choke point. Non-allowlisted chats get dropped here
        # before any AI call or DB write. A single log line per drop lets
        # the user audit and populate the list.
        allowed = self.config.telegram_allowed_chat_ids
        if allowed and chat_id_int not in allowed:
            log.warning(
                "telegram: dropped message from unauthorized chat_id=%s (title=%r, username=%r)",
                chat_id_int,
                chat.get("title"),
                chat.get("username") or chat.get("first_name"),
            )
            return

        if is_command:
            self._handle_command(chat_id_int, cmd)
        else:
            self._capture_text(chat_id_int, text, update.get("update_id"))

    # ------------------------------------------------------------- commands

    def _handle_command(self, chat_id: int, cmd: str) -> None:
        if cmd == "/start":
            self._send_message(
                chat_id,
                "ScaffoldOrganizer 2.0 에 연결되었습니다. 메시지를 보내면 Inbox 로 자동 캡처됩니다.\n\n" + _HELP_TEXT,
            )
            return
        if cmd == "/help":
            self._send_message(chat_id, _HELP_TEXT)
            return
        if cmd == "/stats":
            with connect(self.config) as conn:
                rows = conn.execute(
                    "SELECT status, COUNT(*) AS n FROM items "
                    "WHERE status IN ('inbox','todo','doing') GROUP BY status"
                ).fetchall()
            counts = {row["status"]: int(row["n"]) for row in rows}
            self._send_message(
                chat_id,
                f"📊 현재 상태\n"
                f"Inbox : <b>{counts.get('inbox', 0)}</b>\n"
                f"Active: <b>{counts.get('todo', 0)}</b>\n"
                f"Doing : <b>{counts.get('doing', 0)}</b>",
            )
            return
        if cmd == "/list":
            with connect(self.config) as conn:
                rows = conn.execute(
                    "SELECT id, title FROM items WHERE status='inbox' "
                    "ORDER BY created_at DESC LIMIT 5"
                ).fetchall()
            if not rows:
                self._send_message(chat_id, "Inbox 가 비어있습니다.")
                return
            lines = [f"#{row['id']} · {html.escape(str(row['title']))}" for row in rows]
            self._send_message(chat_id, "<b>최근 Inbox 5개</b>\n" + "\n".join(lines))
            return
        self._send_message(chat_id, f"알 수 없는 명령: <code>{html.escape(cmd)}</code>\n/help 를 참고해주세요.")

    # -------------------------------------------------------- text capture

    def _capture_text(self, chat_id: int, text: str, update_id: Any) -> None:
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
                    external_ref=str(update_id) if update_id is not None else None,
                ),
            )
            record_event(conn, "telegram_received", item_id=item["id"], payload={"chat_id": chat_id})

        # Send a short ack so the user knows the capture landed and where
        # it went. Title is truncated to keep the reply compact.
        title = html.escape(str(item.get("title") or "")[:60])
        horizon = str(item.get("horizon", "now"))
        status = str(item.get("status", "inbox"))
        priority = int(item.get("priority") or 3)
        self._send_message(
            chat_id,
            f"✅ 저장됨 <b>#{item['id']}</b> · {status} / {horizon} · P{priority}\n"
            f"<i>{title}</i>",
        )
