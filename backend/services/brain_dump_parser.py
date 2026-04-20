"""Parse task_structurer's Markdown output into item dicts.

The structurer's output follows a fixed shape (see ``backend/prompts/
task_structurer.md``):

    ## 🍎 브레인덤프 분류 및 구조화
    ...

    ## 🥑 우선순위 조정 & 실행 원자화

    ### 지금 할 일 (Active Now)
    | P1 | task title | 30m |
    | P2 | another    | 1h  |

    ### 장기 보존 (Long-term Backlog)
    - long-term item 1
    - long-term item 2

    ### 생각 / 메모 (Thought Fragments)
    - thought 1
    - note

We split on ``###`` headers and classify each section by keyword, then
extract lines/table rows into item dicts that ``ItemCreate`` will accept.
Keeping this deterministic (no LLM) means worklog/structurer outputs
always decompose the same way for the same Markdown.
"""
from __future__ import annotations

import re
from typing import Any


_HEADER_RE = re.compile(r"^#{2,3}\s+(.+?)\s*$")
_BULLET_RE = re.compile(r"^\s*[-*•]\s+(.+?)\s*$")
_TABLE_SEP_RE = re.compile(r"^\s*\|?[\s\-:|]+\|?\s*$")
_PRIORITY_ONLY_RE = re.compile(r"^[Pp]?[1-5]$")
_TIME_ONLY_RE = re.compile(r"^\d+\s*(분|시간|hr|hrs|h|m|min)?$", re.IGNORECASE)

# Leading priority markers inside a bullet or title, e.g. "[P1] foo",
# "(P2) bar", "P3 - baz", "1. qux". Matches the digit 1–5; preserves the
# rest of the text for use as the item title.
_LEADING_PRIORITY_RE = re.compile(
    r"""^\s*
        [\[\(]?\s*[Pp]?(?P<prio>[1-5])\s*[\]\)]?   # optional bracket + optional P + digit
        \s*[-:.]?\s*                               # optional separator
        (?P<rest>.+?)\s*$
    """,
    re.VERBOSE,
)


def parse_brain_dump_markdown(md: str) -> list[dict[str, Any]]:
    """Split ``md`` into item dicts. Returns [] for empty / header-less input."""
    if not md or not md.strip():
        return []
    items: list[dict[str, Any]] = []
    for header, body in _split_sections(md):
        defaults = _classify_header(header)
        if defaults is None:
            continue
        items.extend(_extract_items(body, defaults))
    return items


# ------------------------------------------------------------------ internals

def _split_sections(md: str) -> list[tuple[str, str]]:
    sections: list[tuple[str, str]] = []
    current_header: str | None = None
    current_body: list[str] = []
    for line in md.splitlines():
        match = _HEADER_RE.match(line)
        if match:
            if current_header is not None:
                sections.append((current_header, "\n".join(current_body)))
            current_header = match.group(1).strip()
            current_body = []
        else:
            if current_header is not None:
                current_body.append(line)
    if current_header is not None:
        sections.append((current_header, "\n".join(current_body)))
    return sections


def _classify_header(header: str) -> dict[str, Any] | None:
    """Map a section header to the item defaults it should produce."""
    lower = header.lower()
    if "active now" in lower or "지금 할 일" in header or "지금 할일" in header:
        return {"item_type": "task", "horizon": "now", "status": "todo"}
    if (
        "long-term" in lower
        or "long term" in lower
        or "backlog" in lower
        or "장기" in header
        or "보존" in header
    ):
        return {"item_type": "task", "horizon": "long_term", "status": "todo"}
    if (
        "thought" in lower
        or "fragment" in lower
        or "생각" in header
        or "메모" in header
        or "note" in lower
    ):
        return {"item_type": "thought", "horizon": "now", "status": "inbox"}
    return None


def _extract_items(body: str, defaults: dict[str, Any]) -> list[dict[str, Any]]:
    items: list[dict[str, Any]] = []
    for title, priority in _table_entries(body):
        entry = {**defaults, "title": title[:120], "content": title}
        if priority is not None:
            entry["priority"] = priority
        items.append(entry)
    for title, priority in _bullet_entries(body):
        entry = {**defaults, "title": title[:120], "content": title}
        if priority is not None:
            entry["priority"] = priority
        items.append(entry)
    return items


def _table_entries(body: str) -> list[tuple[str, int | None]]:
    """Parse a pipe-style Markdown table into (title, priority) pairs.

    task_structurer's Active Now section typically emits
    `| 우선순위 | 작업 | 예상시간 |` with values like P1, P2 — we lift the
    priority out of that column into the item so the user doesn't have to
    re-prioritise in the GUI.
    """

    lines = [
        line for line in (raw.rstrip() for raw in body.splitlines())
        if line.strip().startswith("|")
    ]
    if len(lines) < 2:
        return []
    rows: list[list[str]] = []
    for line in lines:
        if _TABLE_SEP_RE.match(line):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        rows.append(cells)
    # Drop the header row — structurer's tables always have one.
    if rows:
        rows = rows[1:]

    entries: list[tuple[str, int | None]] = []
    for row in rows:
        title, priority = _split_row(row)
        if title:
            entries.append((title, priority))
    return entries


def _split_row(row: list[str]) -> tuple[str | None, int | None]:
    """Extract the title cell and priority cell from a row.

    A row usually has 3 cells (priority / task / time) but order and count
    vary. Heuristic: cells that fullmatch P1..P5 are treated as priority,
    cells that fullmatch a time literal are discarded, the longest of what
    remains is the title.
    """

    cells = [c for c in row if c and c not in {"-", "—", "–"}]
    if not cells:
        return None, None

    priority: int | None = None
    priority_cells: list[str] = []
    for cell in cells:
        if _PRIORITY_ONLY_RE.fullmatch(cell):
            priority_cells.append(cell)
    if priority_cells:
        # First priority-like cell wins; strip the optional P prefix.
        digit = priority_cells[0].lstrip("Pp")
        try:
            parsed = int(digit)
            if 1 <= parsed <= 5:
                priority = parsed
        except ValueError:
            pass

    content_cells = [
        c for c in cells
        if not _PRIORITY_ONLY_RE.fullmatch(c) and not _TIME_ONLY_RE.fullmatch(c)
    ]
    if not content_cells:
        content_cells = cells
    title = max(content_cells, key=len)

    # The title cell itself may embed a leading priority marker ("P1 task"
    # style) when the structurer merges columns. Prefer that if the table
    # didn't already provide one.
    stripped_title, embedded_priority = _strip_leading_priority(title)
    if embedded_priority is not None and priority is None:
        priority = embedded_priority
        title = stripped_title

    return title, priority


def _bullet_entries(body: str) -> list[tuple[str, int | None]]:
    entries: list[tuple[str, int | None]] = []
    for line in body.splitlines():
        match = _BULLET_RE.match(line)
        if not match:
            continue
        raw_title = match.group(1).strip()
        if not raw_title or raw_title in {"(없음)", "(none)", "-", "—"}:
            continue
        title, priority = _strip_leading_priority(raw_title)
        entries.append((title, priority))
    return entries


def _strip_leading_priority(title: str) -> tuple[str, int | None]:
    """If `title` starts with a priority marker (P1, [P2], (3), 1. ...),
    peel it off and return (clean_title, priority)."""
    match = _LEADING_PRIORITY_RE.match(title)
    if not match:
        return title, None
    try:
        priority = int(match.group("prio"))
    except ValueError:
        return title, None
    rest = match.group("rest").strip()
    if not rest:
        # A line that's literally just "P1" isn't a task — keep the
        # original text and skip priority extraction.
        return title, None
    return rest, priority
