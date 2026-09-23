"""Локальный файл с несохранёнными изменениями (исполнитель и положение задачи).

Изменения копятся здесь при взаимодействии с доской и уходят в Jira только по
кнопке «Сохранить в Jira». Положение задачи всегда описывается тремя
параметрами сразу (`startDate`, `endDate`, `effortDays`): и перетаскивание, и
изменение ширины прямоугольника пишут полный набор, см. `upsert_pending_placement`. Каждое реальное изменение состояния сохраняет
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
    """Возвращает {issue_key: {assigneeId?, assigneeName?, startDate?, endDate?, effortDays?}}.

    Три параметра положения (startDate/endDate/effortDays) всегда пишутся и
    удаляются вместе."""
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


def _norm_effort(value: Any) -> float:
    return round(float(value), 4)


def _placement_changed(
    start_date: str | None,
    end_date: str | None,
    effort_days: float,
    original_start_date: str | None,
    original_end_date: str | None,
    original_effort_days: float | None,
) -> bool:
    if (start_date or None) != (original_start_date or None):
        return True
    if (end_date or None) != (original_end_date or None):
        return True
    if original_effort_days is None:
        return True
    return _norm_effort(original_effort_days) != _norm_effort(effort_days)


def upsert_pending_placement(
    root: Path,
    issue_key: str,
    *,
    start_date: str | None,
    end_date: str | None,
    effort_days: float,
    original_start_date: str | None = None,
    original_end_date: str | None = None,
    original_effort_days: float | None = None,
) -> dict[str, dict[str, Any]]:
    """Записывает положение задачи на доске тремя параметрами сразу.

    Любое перемещение или изменение размера прямоугольника описывается одной
    записью: дата начала, дата окончания и продолжительность (`effortDays`).
    Частичных записей не бывает — либо в pending лежат все три значения, либо
    (если задача вернулась ровно к исходному положению) ни одного.
    """
    changes = read_pending(root)
    previous = copy.deepcopy(changes)
    key = (issue_key or "").strip()
    if not key:
        raise ValueError("Нужен issue_key")
    effort = _norm_effort(effort_days)

    item = dict(changes.get(key) or {})
    changed = _placement_changed(
        start_date,
        end_date,
        effort,
        original_start_date,
        original_end_date,
        original_effort_days,
    )
    if changed:
        item["startDate"] = start_date or None
        item["endDate"] = end_date or None
        item["effortDays"] = effort
    else:
        item.pop("startDate", None)
        item.pop("endDate", None)
        item.pop("effortDays", None)

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
