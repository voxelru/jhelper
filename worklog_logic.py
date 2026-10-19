"""Списание времени: ворклоги Jira, сгруппированные по сотрудникам и дням.

Модуль не знает про Flask/HTTP; ворклоги, которых нет в ответе поиска,
дочитывает переданная функция fetch_worklogs.
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, Callable

from board_logic import assignee_identity, cap_window_end, format_display_value, issue_type_identity
from settings import customer_field_id, priorities_cfg, settings_for_issue_type


def worklog_bounds(start: date | None = None, end: date | None = None) -> tuple[date, date]:
    """Окно страницы списаний.

    По умолчанию — две полные недели: прошлая и текущая (с понедельника
    прошлой недели по воскресенье текущей). Одна заданная граница достраивает
    вторую на те же две недели."""
    today = date.today()
    if start and end and end < start:
        start, end = end, start
    if start is None and end is None:
        start = today - timedelta(days=today.weekday() + 7)
        end = start + timedelta(days=13)
    elif end is None:
        end = start + timedelta(days=13)
    elif start is None:
        start = end - timedelta(days=13)
    return start, cap_window_end(start, end)


def _comment_text(raw: Any) -> str:
    """Комментарий ворклога: строка (Server/DC, API v2) или документ ADF (Cloud, API v3)."""
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw.strip()
    if isinstance(raw, dict):
        if raw.get("type") == "text":
            return str(raw.get("text") or "")
        parts = [_comment_text(child) for child in raw.get("content") or []]
        sep = "\n" if raw.get("type") == "doc" else ""
        return sep.join(p for p in parts if p).strip()
    if isinstance(raw, list):
        return " ".join(_comment_text(x) for x in raw).strip()
    return str(raw)


def _worklog_date(raw: Any) -> date | None:
    """День ворклога из поля started («2026-10-12T09:00:00.000+0300»)."""
    text = str(raw or "")[:10]
    try:
        return date.fromisoformat(text)
    except ValueError:
        return None


def issue_worklogs(
    issue: dict[str, Any],
    fetch_worklogs: Callable[[str], list[dict[str, Any]]],
) -> list[dict[str, Any]]:
    """Все ворклоги задачи: вложенные в ответ поиска или, если их там не все, — дочитанные."""
    embedded = (issue.get("fields") or {}).get("worklog") or {}
    worklogs = list(embedded.get("worklogs") or [])
    total = embedded.get("total")
    if total is not None and int(total) > len(worklogs):
        worklogs = fetch_worklogs(str(issue.get("key")))
    return worklogs


def build_worklog_board(
    raw_issues: list[dict[str, Any]],
    fetch_worklogs: Callable[[str], list[dict[str, Any]]],
    settings: dict[str, Any],
    start: date,
    end: date,
) -> dict[str, Any]:
    """Строки сотрудников с часами по задачам и дням окна [start, end].

    Несколько ворклогов одного сотрудника по одной задаче за один день
    складываются в одну запись (секунды суммируются, комментарии
    собираются списком). Строки — только сотрудники, списавшие время в окне.
    """
    _order, colors = priorities_cfg(settings)
    issues: dict[str, dict[str, Any]] = {}
    rows: dict[str, dict[str, Any]] = {}

    for issue in raw_issues:
        key = str(issue.get("key") or "")
        if not key:
            continue
        fields = issue.get("fields") or {}
        found = False
        for worklog in issue_worklogs(issue, fetch_worklogs):
            day = _worklog_date(worklog.get("started"))
            seconds = int(worklog.get("timeSpentSeconds") or 0)
            if day is None or not (start <= day <= end) or seconds <= 0:
                continue
            author_id, author_name = assignee_identity(worklog.get("author"))
            author_id = author_id or "—"
            row = rows.setdefault(
                author_id,
                {"authorId": author_id, "authorName": author_name or author_id, "entries": {}},
            )
            entry = row["entries"].setdefault(
                (key, day.isoformat()),
                {"key": key, "date": day.isoformat(), "seconds": 0, "comments": []},
            )
            entry["seconds"] += seconds
            comment = _comment_text(worklog.get("comment"))
            if comment:
                entry["comments"].append(comment)
            found = True
        if not found:
            continue

        type_name, type_id = issue_type_identity(fields.get("issuetype"))
        type_settings = settings_for_issue_type(settings, type_name, type_id)
        cust_fid = customer_field_id(type_settings)
        status = fields.get("status") or {}
        status_category = status.get("statusCategory") or {} if isinstance(status, dict) else {}
        priority = (fields.get("priority") or {}).get("name")
        issues[key] = {
            "key": key,
            "summary": (fields.get("summary") or "").strip() or key,
            "status": status.get("name") if isinstance(status, dict) else None,
            "statusCategory": status_category.get("key") if isinstance(status_category, dict) else None,
            "issueType": type_name,
            "priority": priority,
            "customer": format_display_value(fields.get(cust_fid)) if cust_fid else None,
            "color": colors.get(priority) or colors.get("default") or "#78909c",
        }

    out_rows = []
    for row in rows.values():
        entries = sorted(row["entries"].values(), key=lambda e: (e["date"], e["key"]))
        out_rows.append(
            {
                "authorId": row["authorId"],
                "authorName": row["authorName"],
                "totalSeconds": sum(e["seconds"] for e in entries),
                "entries": entries,
            }
        )
    out_rows.sort(key=lambda r: (r["authorName"] or "").casefold())

    # Выходные на шкале не показываются, если в них никто не списывал время.
    logged_days = {e["date"] for r in out_rows for e in r["entries"]}
    days = []
    d = start
    while d <= end:
        weekend = d.weekday() >= 5
        if not weekend or d.isoformat() in logged_days:
            days.append({"date": d.isoformat(), "weekend": weekend})
        d += timedelta(days=1)

    return {"rows": out_rows, "issues": issues, "days": days}
