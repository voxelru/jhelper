"""Локальный текстовый файл с несохранёнными сменами исполнителя."""

from __future__ import annotations

from pathlib import Path
from typing import Any

PENDING_FILENAME = "pending_assignee_changes.txt"
HEADER = (
    "# Несохранённые смены исполнителя (issue_key\\tassignee_id\\tassignee_name).\n"
    "# Записываются при перетаскивании; в Jira уходят только по кнопке «Сохранить».\n"
)


def pending_path(root: Path) -> Path:
    return root / PENDING_FILENAME


def read_pending(root: Path) -> dict[str, dict[str, str]]:
    """Возвращает {issue_key: {assigneeId, assigneeName}}."""
    path = pending_path(root)
    if not path.exists():
        return {}
    out: dict[str, dict[str, str]] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split("\t")
        if len(parts) < 2:
            continue
        key = parts[0].strip()
        assignee_id = parts[1].strip()
        assignee_name = parts[2].strip() if len(parts) > 2 else assignee_id
        if key and assignee_id:
            out[key] = {"assigneeId": assignee_id, "assigneeName": assignee_name or assignee_id}
    return out


def write_pending(root: Path, changes: dict[str, dict[str, str]]) -> None:
    path = pending_path(root)
    lines = [HEADER]
    for key in sorted(changes):
        item = changes[key]
        aid = str(item.get("assigneeId") or "").strip()
        aname = str(item.get("assigneeName") or aid).strip()
        if not aid:
            continue
        lines.append(f"{key}\t{aid}\t{aname}\n")
    if len(lines) == 1:
        # только заголовок — очищаем файл, но оставляем пустым с комментарием
        path.write_text(HEADER, encoding="utf-8")
        return
    path.write_text("".join(lines), encoding="utf-8")


def upsert_pending(
    root: Path,
    issue_key: str,
    assignee_id: str,
    assignee_name: str,
    *,
    original_assignee_id: str | None = None,
) -> dict[str, dict[str, str]]:
    """Добавляет/обновляет смену; если вернулись к исходному — удаляет запись."""
    changes = read_pending(root)
    key = (issue_key or "").strip()
    aid = (assignee_id or "").strip()
    aname = (assignee_name or aid).strip()
    if not key or not aid:
        raise ValueError("Нужны issue_key и assignee_id")

    if original_assignee_id is not None and str(original_assignee_id) == aid:
        changes.pop(key, None)
    else:
        changes[key] = {"assigneeId": aid, "assigneeName": aname}
    write_pending(root, changes)
    return changes


def clear_pending(root: Path) -> None:
    write_pending(root, {})


def pending_list(changes: dict[str, dict[str, str]]) -> list[dict[str, Any]]:
    return [
        {
            "key": key,
            "assigneeId": item["assigneeId"],
            "assigneeName": item["assigneeName"],
        }
        for key, item in sorted(changes.items())
    ]
