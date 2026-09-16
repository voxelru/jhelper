"""Локальный файл с несохранёнными изменениями (исполнитель, трудозатраты).

Изменения копятся здесь при взаимодействии с доской (drag-and-drop смены
исполнителя, изменение ширины прямоугольника) и уходят в Jira только по
кнопке «Сохранить в Jira».
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

PENDING_FILENAME = "pending_changes.json"


def pending_path(root: Path) -> Path:
    return root / PENDING_FILENAME


def read_pending(root: Path) -> dict[str, dict[str, Any]]:
    """Возвращает {issue_key: {assigneeId?, assigneeName?, effortDays?}}."""
    path = pending_path(root)
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "{}")
    except (json.JSONDecodeError, OSError):
        return {}
    if not isinstance(raw, dict):
        return {}
    out: dict[str, dict[str, Any]] = {}
    for key, item in raw.items():
        if isinstance(item, dict) and item:
            out[str(key)] = dict(item)
    return out


def write_pending(root: Path, changes: dict[str, dict[str, Any]]) -> None:
    path = pending_path(root)
    cleaned = {key: item for key, item in changes.items() if item}
    if not cleaned:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")


def upsert_pending_assignee(
    root: Path,
    issue_key: str,
    assignee_id: str,
    assignee_name: str,
    *,
    original_assignee_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Добавляет/обновляет смену исполнителя; если вернулись к исходному — убирает её."""
    changes = read_pending(root)
    key = (issue_key or "").strip()
    aid = (assignee_id or "").strip()
    aname = (assignee_name or aid).strip()
    if not key or not aid:
        raise ValueError("Нужны issue_key и assignee_id")

    item = dict(changes.get(key) or {})
    if original_assignee_id is not None and str(original_assignee_id) == aid:
        item.pop("assigneeId", None)
        item.pop("assigneeName", None)
    else:
        item["assigneeId"] = aid
        item["assigneeName"] = aname

    if item:
        changes[key] = item
    else:
        changes.pop(key, None)
    write_pending(root, changes)
    return changes


def upsert_pending_effort(
    root: Path,
    issue_key: str,
    effort_days: float,
    *,
    original_effort_days: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Добавляет/обновляет плановые трудозатраты; если вернулись к исходным — убирает их."""
    changes = read_pending(root)
    key = (issue_key or "").strip()
    if not key:
        raise ValueError("Нужен issue_key")

    item = dict(changes.get(key) or {})
    if original_effort_days is not None and round(float(original_effort_days), 4) == round(float(effort_days), 4):
        item.pop("effortDays", None)
    else:
        item["effortDays"] = round(float(effort_days), 4)

    if item:
        changes[key] = item
    else:
        changes.pop(key, None)
    write_pending(root, changes)
    return changes


def clear_pending(root: Path) -> None:
    write_pending(root, {})


def pending_list(changes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"key": key, **item} for key, item in sorted(changes.items())]
