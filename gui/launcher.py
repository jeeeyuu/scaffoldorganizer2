from __future__ import annotations

import json
import os
import platform
import subprocess
import sys
import time
from pathlib import Path

import requests
import webview

ROOT_DIR = Path(__file__).resolve().parents[1]
BACKEND_MODE = os.getenv("SCAFFOLD_BACKEND_MODE", "wsl").lower()
READY_TIMEOUT_SECONDS = float(os.getenv("SCAFFOLD_BACKEND_READY_TIMEOUT", "45"))


def _config_dir() -> Path:
    """Locate config/ either next to a PyInstaller exe (production) or under
    the repo root (development)."""

    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent / "config"
    return ROOT_DIR / "config"


def _load_runtime_config() -> dict:
    """Minimal raw read of config.json.

    Deliberately avoids importing backend.config so pywebview does not pull
    pydantic/openai into the GUI-side dependency set (keeps the .exe lean).
    Values read here are only those the launcher itself needs — the backend
    parses the full AppConfig on its own side."""

    path = _config_dir() / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {}


_RUNTIME_CONFIG = _load_runtime_config()
BACKEND_HOST = _RUNTIME_CONFIG.get("backend_host", "127.0.0.1")
# GUI always reaches the backend over loopback even when backend_host is
# 0.0.0.0 (docker default).
GUI_BACKEND_HOST = "127.0.0.1" if BACKEND_HOST == "0.0.0.0" else BACKEND_HOST
BACKEND_PORT = int(_RUNTIME_CONFIG.get("backend_port", 8765))
BACKEND_URL = os.getenv("SCAFFOLD_BACKEND_URL", f"http://{GUI_BACKEND_HOST}:{BACKEND_PORT}")


def _backend_command() -> list[str] | None:
    """Return the subprocess command to launch the backend, or None if the
    backend is managed externally. Raises for unknown modes or missing
    required configuration."""

    if BACKEND_MODE == "external":
        return None

    if BACKEND_MODE == "docker":
        return ["docker", "compose", "up", "--build", "backend"]

    if BACKEND_MODE == "wsl":
        if platform.system().lower() == "windows":
            entrypoint = (
                os.getenv("SCAFFOLD_WSL_ENTRYPOINT")
                or _RUNTIME_CONFIG.get("wsl_backend_entrypoint", "")
            ).strip()
            dist = (
                os.getenv("SCAFFOLD_WSL_DIST")
                or _RUNTIME_CONFIG.get("wsl_distribution_name", "")
            ).strip()
            if not entrypoint:
                raise RuntimeError(
                    "Windows GUI → WSL backend requires `wsl_backend_entrypoint` in "
                    f"{_config_dir() / 'config.json'} (or SCAFFOLD_WSL_ENTRYPOINT env var). "
                    "It must be the POSIX path to the `developing/` folder *inside* WSL."
                )
            uvicorn_cmd = (
                f"cd {entrypoint} && "
                f"python3 -m uvicorn backend.main:app "
                f"--host {BACKEND_HOST} --port {BACKEND_PORT}"
            )
            command: list[str] = ["wsl.exe"]
            if dist:
                command += ["-d", dist]
            command += ["bash", "-lc", uvicorn_cmd]
            return command

        # Non-Windows: run uvicorn directly in the current Python interpreter.
        return [
            sys.executable,
            "-m",
            "uvicorn",
            "backend.main:app",
            "--host",
            BACKEND_HOST,
            "--port",
            str(BACKEND_PORT),
        ]

    raise ValueError(f"Unknown SCAFFOLD_BACKEND_MODE: {BACKEND_MODE!r}")


class BackendProcess:
    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None

    def start(self) -> None:
        if self._is_healthy():
            return
        command = _backend_command()
        if command is None:
            self._wait_until_ready()
            return
        self.process = subprocess.Popen(command, cwd=str(ROOT_DIR))
        self._wait_until_ready()

    def stop(self) -> None:
        # Ask the backend to shut down gracefully so Telegram polling drains.
        try:
            requests.post(f"{BACKEND_URL}/shutdown", timeout=1)
        except requests.RequestException:
            pass
        if self.process is None:
            return
        try:
            self.process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self.process.terminate()
            try:
                self.process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.process.kill()

    def _wait_until_ready(self) -> None:
        deadline = time.time() + READY_TIMEOUT_SECONDS
        while time.time() < deadline:
            if self._is_healthy():
                return
            time.sleep(0.35)
        raise RuntimeError(
            f"Backend did not become healthy within {READY_TIMEOUT_SECONDS:.0f} seconds "
            f"(mode={BACKEND_MODE}, url={BACKEND_URL})."
        )

    def _is_healthy(self) -> bool:
        try:
            return requests.get(f"{BACKEND_URL}/health", timeout=0.5).ok
        except requests.RequestException:
            return False


def _index_html_uri() -> str:
    """index.html lives inside the PyInstaller bundle at runtime when frozen,
    and under the source tree otherwise."""

    if getattr(sys, "frozen", False):
        bundle_root = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
        return (bundle_root / "gui" / "index.html").as_uri()
    return (ROOT_DIR / "gui" / "index.html").as_uri()


def _window_size() -> tuple[int, int]:
    """Default window size from config. 600x400 is intentionally compact —
    the UI is designed to scroll vertically and never horizontally."""

    pw = _RUNTIME_CONFIG.get("pywebview", {}) or {}
    try:
        width = int(pw.get("width", 600))
        height = int(pw.get("height", 400))
    except (TypeError, ValueError):
        width, height = 600, 400
    return max(320, width), max(240, height)


def main() -> None:
    backend = BackendProcess()
    backend.start()
    width, height = _window_size()
    title = _RUNTIME_CONFIG.get("app_name") or "ScaffoldOrganizer 2.0"
    webview.create_window(
        title,
        _index_html_uri(),
        width=width,
        height=height,
        min_size=(320, 240),
    )
    try:
        webview.start()
    finally:
        backend.stop()


if __name__ == "__main__":
    main()
