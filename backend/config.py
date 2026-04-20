from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field, ValidationError, field_validator

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - optional convenience dependency
    def load_dotenv(*_args: object, **_kwargs: object) -> bool:
        return False


ROOT_DIR = Path(__file__).resolve().parents[1]
CONFIG_DIR = ROOT_DIR / "config"

ResponseFormat = Literal["json", "markdown", "text"]
AI_ROLE_NAMES = ("command_router", "classifier", "task_structurer", "worklog_writer")


class AIRoleConfig(BaseModel):
    """Per-role AI settings — intentionally minimal.

    When `prompt_id` is set (the normal case), the OpenAI dashboard owns
    everything about the call: model, system/developer message, temperature,
    max tokens, response format, tools, etc. The backend only sends the
    user message and reads the reply. We therefore do NOT accept model /
    temperature / max_output_tokens here — passing them over the wire
    just fights the dashboard config (and some reasoning models 400 on
    `temperature` entirely, which is what triggered the cleanup).

    `response_format` stays because it's a *client-side* parsing hint
    (JSON vs Markdown), not an API parameter. `prompt_file` stays as the
    offline-dev fallback path when prompt_id is empty."""

    prompt_id: str = ""
    prompt_file: str = ""
    response_format: ResponseFormat = "json"


class PyWebviewConfig(BaseModel):
    width: int = 1280
    height: int = 860


class AppConfig(BaseModel):
    app_name: str = "ScaffoldOrganizer 2.0"
    backend_host: str = "127.0.0.1"
    backend_port: int = Field(default=8765, ge=1, le=65535)
    debug: bool = False
    db_path: str = "data/scaffold_workbench.sqlite3"
    export_dir: str = "exports"
    markdown_export_dir: str = "exports/markdown"
    worklog_export_dir: str = "exports/worklogs"
    session_export_dir: str = "exports/sessions"
    log_dir: str = "logs"
    telegram_enabled: bool = False
    telegram_bot_token: str = ""
    telegram_allowed_chat_ids: list[int] = Field(default_factory=list)
    telegram_poll_interval_seconds: float = Field(default=1.0, ge=0.2)
    openai_api_key: str = ""
    ai_roles: dict[str, AIRoleConfig] = Field(default_factory=dict)
    wsl_backend_entrypoint: str = ""
    wsl_distribution_name: str = ""
    ui_refresh_interval_ms: int = Field(default=2000, ge=500)
    pywebview: PyWebviewConfig = Field(default_factory=PyWebviewConfig)

    @field_validator("telegram_bot_token")
    @classmethod
    def token_required_when_enabled(cls, value: str, info: Any) -> str:
        enabled = bool(info.data.get("telegram_enabled", False))
        if enabled and not value.strip():
            raise ValueError("telegram_enabled=true requires telegram_bot_token")
        return value

    @field_validator("ai_roles")
    @classmethod
    def required_roles_present(cls, value: dict[str, AIRoleConfig]) -> dict[str, AIRoleConfig]:
        missing = [name for name in AI_ROLE_NAMES if name not in value]
        if missing:
            raise ValueError(f"ai_roles missing required entries: {missing}")
        return value

    def role(self, name: str) -> AIRoleConfig:
        try:
            return self.ai_roles[name]
        except KeyError as exc:
            raise KeyError(f"Unknown AI role: {name}") from exc

    @property
    def backend_url(self) -> str:
        return f"http://{self.backend_host}:{self.backend_port}"


def resolve_path(value: str | Path) -> Path:
    path = Path(value).expanduser()
    if not path.is_absolute():
        path = ROOT_DIR / path
    return path


def load_config(config_path: str | Path | None = None) -> AppConfig:
    load_dotenv(ROOT_DIR / ".env")
    path = Path(config_path) if config_path else CONFIG_DIR / "config.json"
    if not path.exists():
        example = CONFIG_DIR / "config_example.json"
        raise RuntimeError(
            f"Missing config file: {path}. Create it from {example} and fill local values."
        )

    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in {path}: {exc}") from exc

    raw["telegram_bot_token"] = os.getenv("TELEGRAM_BOT_TOKEN", raw.get("telegram_bot_token", ""))
    raw["openai_api_key"] = os.getenv("OPENAI_API_KEY", raw.get("openai_api_key", ""))

    try:
        config = AppConfig.model_validate(raw)
    except ValidationError as exc:
        raise RuntimeError(f"Invalid config: {exc}") from exc

    for directory in [
        config.export_dir,
        config.markdown_export_dir,
        config.worklog_export_dir,
        config.session_export_dir,
        config.log_dir,
        str(Path(config.db_path).parent),
    ]:
        resolve_path(directory).mkdir(parents=True, exist_ok=True)
    return config
