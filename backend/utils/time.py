from __future__ import annotations

from datetime import datetime


def utc_now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def local_date() -> str:
    return datetime.now().date().isoformat()
