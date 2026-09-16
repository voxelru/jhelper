"""Локальный файл с несохранёнными изменениями (исполнитель, трудозатраты, даты).

Изменения копятся здесь при взаимодействии с доской (drag-and-drop смены
исполнителя/даты, изменение ширины прямоугольника) и уходят в Jira только по
кнопке «Сохранить в Jira». Каждое реальное изменение состояния сохраняет
предыдущий снимок в историю (`pending_changes_history.json`), что позволяет
отменить последнее действие (`undo_last`) или все несохранённые изменения
разом (`clear_pending`).
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

PENDING_FILENAME = "pending_changes.json"
HISTORY_FILENAME = "pending_changes_history.json"
MAX_HISTORY = 20


def pending_path(root: Path) -> Path:
    return root / PENDING_FILENAME


def history_path(root: Path) -> Path:
    return root / HISTORY_FILENAME


def _read_history(root: Path) -> list[dict[str, dict[str, Any]]]:
    path = history_path(root)
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text(encoding="utf-8") or "[]")
    except (json.JSONDecodeError, OSError):
        return []
    return raw if isinstance(raw, list) else []


def _write_history(root: Path, history: list[dict[str, dict[str, Any]]]) -> None:
    path = history_path(root)
    if not history:
        if path.exists():
            path.unlink()
        return
    path.write_text(json.dumps(history, ensure_ascii=False), encoding="utf-8")


def _push_history(root: Path, previous_state: dict[str, dict[str, Any]]) -> None:
    history = _read_history(root)
    history.append(previous_state)
    if len(history) > MAX_HISTORY:
        history = history[-MAX_HISTORY:]
    _write_history(root, history)


def _commit(
    root: Path,
    previous: dict[str, dict[str, Any]],
    changes: dict[str, dict[str, Any]],
) -> dict[str, dict[str, Any]]:
    """Сохраняет новое состояние, если оно реально отличается, и запоминает предыдущее в истории."""
    if changes != previous:
        _push_history(root, previous)
        write_pending(root, changes)
    return changes


def undo_last(root: Path) -> dict[str, dict[str, Any]]:
    """Откатывает последнее изменение (возвращает предыдущее состояние pending-изменений)."""
    history = _read_history(root)
    if not history:
        return read_pending(root)
    previous_state = history.pop()
    _write_history(root, history)
    write_pending(root, previous_state)
    return previous_state


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
    previous = copy.deepcopy(changes)
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
    return _commit(root, previous, changes)


def upsert_pending_effort(
    root: Path,
    issue_key: str,
    effort_days: float,
    *,
    original_effort_days: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Добавляет/обновляет плановые трудозатраты; если вернулись к исходным — убирает их."""
    changes = read_pending(root)
    previous = copy.deepcopy(changes)
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
    return _commit(root, previous, changes)


def upsert_pending_dates(
    root: Path,
    issue_key: str,
    *,
    has_start: bool = False,
    start_date: str | None = None,
    original_start_date: str | None = None,
    has_end: bool = False,
    end_date: str | None = None,
    original_end_date: str | None = None,
) -> dict[str, dict[str, Any]]:
    """Добавляет/обновляет дату начала и/или окончания; если вернулись к исходной — убирает её.

    has_start/has_end различают «поле не прислали» (не трогаем) от «прислали пустое значение».
    """
    changes = read_pending(root)
    previous = copy.deepcopy(changes)
    key = (issue_key or "").strip()
    if not key:
        raise ValueError("Нужен issue_key")

    item = dict(changes.get(key) or {})

    if has_start:
        if start_date and start_date != original_start_date:
            item["startDate"] = start_date
        else:
            item.pop("startDate", None)

    if has_end:
        if end_date and end_date != original_end_date:
            item["endDate"] = end_date
        else:
            item.pop("endDate", None)

    if item:
        changes[key] = item
    else:
        changes.pop(key, None)
    return _commit(root, previous, changes)


def reset_history(root: Path) -> None:
    """Стирает историю undo (вызывается после сохранения в Jira — старые записи
    истории после этого ссылались бы на уже применённые значения)."""
    _write_history(root, [])


def clear_pending(root: Path) -> None:
    """Отменяет все несохранённые изменения (запоминая их в истории для undo_last)."""
    previous = read_pending(root)
    if previous:
        _push_history(root, previous)
    write_pending(root, {})


def pending_list(changes: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    return [{"key": key, **item} for key, item in sorted(changes.items())]
