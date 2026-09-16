"""Бизнес-логика доски: нормализация задач Jira, раскладка по строкам,
трудозатраты и даты. Модуль не знает про Flask/HTTP.
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from typing import Any

from settings import (
    customer_field_id,
    date_field_cfg,
    effort_cfg,
    horizon_cfg,
    priorities_cfg,
    sprint_field_id,
)

# Системные поля Jira, которые нужны всегда вне зависимости от настроек.
BASE_JIRA_FIELDS = ["summary", "assignee", "priority", "status", "timetracking"]


def planning_bounds(settings: dict[str, Any]) -> tuple[date, date]:
    kind, count = horizon_cfg(settings)
    start = date.today()
    if kind == "working_days":
        collected: list[date] = []
        d = start
        while len(collected) < count:
            if d.weekday() < 5:
                collected.append(d)
            d += timedelta(days=1)
        end = collected[-1] if collected else start
        return start, end
    return start, start + timedelta(days=count - 1)


def iter_working_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def working_days_between(start: date, end: date) -> int:
    """Число рабочих дней от start (включительно) до end (не включая), знак — как у (end-start)."""
    if end == start:
        return 0
    sign = 1
    a, b = start, end
    if end < start:
        a, b = end, start
        sign = -1
    count = 0
    d = a
    while d < b:
        if d.weekday() < 5:
            count += 1
        d += timedelta(days=1)
    return sign * count


def effort_seconds_to_days(seconds: int | None, settings: dict[str, Any]) -> float:
    min_d = float(settings.get("min_effort_working_days", 1))
    if not seconds or seconds <= 0:
        return min_d
    hours = seconds / 3600.0
    wh = float(settings.get("working_hours_per_day", 8))
    return max(min_d, hours / wh)


def _flatten_field_value(raw: Any) -> Any:
    """Приводит сырое значение кастомного поля Jira к простому числу/строке."""
    if raw is None:
        return None
    if isinstance(raw, (int, float, str, bool)):
        return raw
    if isinstance(raw, dict):
        if "value" in raw and isinstance(raw["value"], (int, float, str)):
            return raw["value"]
        if "amount" in raw:
            return raw.get("amount")
        if raw.get("type") == "number" and "number" in raw:
            return raw.get("number")
    return raw


def format_display_value(raw: Any) -> str | None:
    """Человекочитаемое значение кастомного поля Jira (заказчик и т.п.)."""
    if raw is None:
        return None
    if isinstance(raw, bool):
        return "да" if raw else "нет"
    if isinstance(raw, (int, float)):
        return str(raw)
    if isinstance(raw, str):
        text = raw.strip()
        return text or None
    if isinstance(raw, list):
        parts = [format_display_value(x) for x in raw]
        joined = ", ".join(p for p in parts if p)
        return joined or None
    if isinstance(raw, dict):
        for key in ("displayName", "value", "name", "label", "emailAddress"):
            if raw.get(key):
                return str(raw[key]).strip() or None
        if "child" in raw:
            return format_display_value(raw.get("child"))
    return str(raw)


# Форматы даты, которые Jira может возвращать в кастомных полях, помимо ISO.
_DATE_FORMATS = ("%Y-%m-%d", "%d.%m.%Y", "%d/%m/%Y", "%Y/%m/%d")


def parse_jira_date(raw: Any) -> str | None:
    """Возвращает дату в ISO YYYY-MM-DD или None, если не удалось распознать."""
    if raw is None:
        return None
    if isinstance(raw, dict):
        raw = raw.get("value") or raw.get("date") or raw.get("startDate") or raw.get("endDate")
    if isinstance(raw, (int, float)):
        return None
    text = str(raw).strip()
    if not text:
        return None
    if len(text) >= 10 and text[4] == "-" and text[7] == "-":
        return text[:10]
    for fmt in _DATE_FORMATS:
        try:
            return datetime.strptime(text[:10], fmt).date().isoformat()
        except ValueError:
            continue
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).date().isoformat()
    except ValueError:
        return None


def effort_days_for_issue(
    fields: dict[str, Any],
    effort_type: str,
    field_id: str | None,
    settings: dict[str, Any],
) -> float:
    min_d = float(settings.get("min_effort_working_days", 1))

    if effort_type == "timetracking_remaining":
        tt = fields.get("timetracking") or {}
        return effort_seconds_to_days(tt.get("remainingEstimateSeconds"), settings)
    if effort_type == "timetracking_original":
        tt = fields.get("timetracking") or {}
        return effort_seconds_to_days(tt.get("originalEstimateSeconds"), settings)

    if not field_id:
        return min_d

    raw = _flatten_field_value(fields.get(field_id))

    if effort_type == "seconds_field":
        try:
            sec = int(float(raw)) if raw is not None else 0
        except (TypeError, ValueError):
            sec = 0
        return effort_seconds_to_days(sec, settings)

    if effort_type == "number_field":
        try:
            n = float(raw) if raw is not None else 0.0
        except (TypeError, ValueError):
            n = 0.0
        return max(min_d, n)

    return min_d


def effort_days_to_minutes(days: float, settings: dict[str, Any]) -> int:
    wh = float(settings.get("working_hours_per_day", 8))
    return max(1, round(float(days) * wh * 60.0))


def effort_days_to_seconds(days: float, settings: dict[str, Any]) -> int:
    wh = float(settings.get("working_hours_per_day", 8))
    return max(1, round(float(days) * wh * 3600.0))


def effort_days_to_jira_raw(days: float, effort_type: str, settings: dict[str, Any]) -> Any:
    """Готовит значение трудозатрат в формате, который ожидает Jira при записи."""
    if effort_type in ("timetracking_original", "timetracking_remaining"):
        return f"{effort_days_to_minutes(days, settings)}m"
    if effort_type == "seconds_field":
        return effort_days_to_seconds(days, settings)
    if effort_type == "number_field":
        return round(float(days), 2)
    raise ValueError(f"Сохранение трудозатрат не поддерживается для типа {effort_type}")


def compute_pinned_offset(
    planning_start: date,
    jira_start: str | None,
    jira_end: str | None,
    effort_days: float,
) -> float | None:
    """Смещение (в рабочих днях от planning_start), на которое «прибита» задача,
    если у неё назначена дата начала (или хотя бы окончания) в Jira."""
    if jira_start:
        return max(0.0, float(working_days_between(planning_start, date.fromisoformat(jira_start))))
    if jira_end:
        end_offset = working_days_between(planning_start, date.fromisoformat(jira_end)) + 1
        return max(0.0, float(end_offset) - effort_days)
    return None


def parse_sprint_value(raw: Any) -> str | None:
    """Достаёт имя (текущего/последнего) спринта из поля Jira Sprint.

    Поддерживает и современный формат (список словарей с name/state),
    и устаревший строковый формат Jira Server/DC
    ("com.atlassian.greenhopper...Sprint@...[...,name=Спринт 5,...]").
    """
    if raw is None:
        return None
    if isinstance(raw, list):
        for item in reversed(raw):
            name = parse_sprint_value(item)
            if name:
                return name
        return None
    if isinstance(raw, dict):
        name = raw.get("name")
        return str(name).strip() or None if name else None
    text = str(raw).strip()
    if not text:
        return None
    m = re.search(r"name=([^,\]]+)", text)
    if m:
        return m.group(1).strip() or None
    return text


def _is_positive_number(raw: Any) -> bool:
    try:
        return raw is not None and float(raw) > 0
    except (TypeError, ValueError):
        return False


def has_jira_effort(fields: dict[str, Any], effort_type: str, field_id: str | None) -> bool:
    if effort_type == "timetracking_remaining":
        tt = fields.get("timetracking") or {}
        return _is_positive_number(tt.get("remainingEstimateSeconds"))
    if effort_type == "timetracking_original":
        tt = fields.get("timetracking") or {}
        return _is_positive_number(tt.get("originalEstimateSeconds"))
    if not field_id:
        return False
    raw = _flatten_field_value(fields.get(field_id))
    return _is_positive_number(raw)


def assignee_identity(assignee: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """Ключ строки и отображаемое имя (Jira Cloud / Server)."""
    if not assignee:
        return None, None
    key = assignee.get("accountId") or assignee.get("name") or assignee.get("key") or assignee.get("id")
    if key is not None:
        key = str(key)
    name = assignee.get("displayName") or assignee.get("name") or key
    return key, str(name) if name else key


def priority_rank(name: str | None, order: list[str]) -> int:
    if not name:
        return len(order) + 99
    try:
        return order.index(name)
    except ValueError:
        return len(order) + 50


def collect_jira_fields(settings: dict[str, Any]) -> list[str]:
    """Список полей Jira для запроса: системные + настроенные в effort/fields."""
    fields = list(BASE_JIRA_FIELDS)
    effort_type, effort_field_id = effort_cfg(settings)
    if effort_type in ("number_field", "seconds_field") and effort_field_id:
        fields.append(effort_field_id)

    cust = customer_field_id(settings)
    if cust:
        fields.append(cust)

    sprint_fid = sprint_field_id(settings)
    if sprint_fid:
        fields.append(sprint_fid)

    for name in ("start_date", "end_date"):
        fid = date_field_cfg(settings, name)
        if fid:
            fields.append(fid)

    seen: set[str] = set()
    out: list[str] = []
    for f in fields:
        if f not in seen:
            seen.add(f)
            out.append(f)
    return out


def fields_public_cfg(s: dict[str, Any]) -> dict[str, Any]:
    """Часть настроек полей, которую отдаём на фронтенд (для тултипа/подписей)."""
    return {
        "customer": {"jiraFieldId": customer_field_id(s)},
        "sprint": {"jiraFieldId": sprint_field_id(s)},
        "startDate": {"jiraFieldId": date_field_cfg(s, "start_date")},
        "endDate": {"jiraFieldId": date_field_cfg(s, "end_date")},
    }


def normalize_issues(raw_issues: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Превращает сырые issues Jira в задачи доски (без раскладки по строкам)."""
    effort_type, effort_field_id = effort_cfg(settings)
    order, colors = priorities_cfg(settings)
    cust_fid = customer_field_id(settings)
    sprint_fid = sprint_field_id(settings)
    start_fid = date_field_cfg(settings, "start_date")
    end_fid = date_field_cfg(settings, "end_date")
    planning_start, _planning_end = planning_bounds(settings)

    tasks: list[dict[str, Any]] = []
    for issue in raw_issues:
        key = issue.get("key")
        fields = issue.get("fields") or {}
        summary = (fields.get("summary") or "").strip() or key
        pr = fields.get("priority") or {}
        priority_name = pr.get("name")
        status = fields.get("status") or {}
        status_name = status.get("name") if isinstance(status, dict) else None
        assignee = fields.get("assignee")
        assignee_id, assignee_name = assignee_identity(assignee if isinstance(assignee, dict) else None)
        if not assignee_id:
            continue

        effort_days = effort_days_for_issue(fields, effort_type, effort_field_id, settings)
        color = colors.get(priority_name) or colors.get("default") or "#78909c"
        customer = format_display_value(fields.get(cust_fid)) if cust_fid else None
        sprint = parse_sprint_value(fields.get(sprint_fid)) if sprint_fid else None

        jira_start = parse_jira_date(fields.get(start_fid)) if start_fid else None
        jira_end = parse_jira_date(fields.get(end_fid)) if end_fid else None

        # Задача «в приоритете» по срокам: если в Jira назначена дата начала (или
        # только окончания), задача встаёт на диаграмме на неё, а не в очередь.
        pinned_offset = compute_pinned_offset(planning_start, jira_start, jira_end, effort_days)

        missing_fields: list[str] = []
        if start_fid and not parse_jira_date(fields.get(start_fid)):
            missing_fields.append("дата начала")
        if end_fid and not parse_jira_date(fields.get(end_fid)):
            missing_fields.append("дата окончания")
        if not has_jira_effort(fields, effort_type, effort_field_id):
            missing_fields.append("трудозатраты")

        tasks.append(
            {
                "key": key,
                "summary": summary,
                "status": status_name,
                "customer": customer,
                "sprint": sprint,
                "priority": priority_name,
                "assigneeId": assignee_id,
                "assigneeName": assignee_name,
                "originalAssigneeId": assignee_id,
                "originalAssigneeName": assignee_name,
                "pendingAssignee": False,
                "pendingEffort": False,
                "effortDays": round(effort_days, 4),
                "originalEffortDays": round(effort_days, 4),
                "pendingDates": False,
                "color": color,
                "jiraStartDate": jira_start,
                "jiraEndDate": jira_end,
                "originalJiraStartDate": jira_start,
                "originalJiraEndDate": jira_end,
                "pinnedStartOffsetDays": round(pinned_offset, 4) if pinned_offset is not None else None,
                "missingFields": missing_fields,
                "_rank": priority_rank(priority_name, order),
            }
        )

    tasks.sort(key=lambda t: (t["assigneeId"] or "", t["_rank"], t["key"]))
    for t in tasks:
        del t["_rank"]
    return tasks


def _without_pinned_marker(item: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in item.items() if k != "pinnedStartOffsetDays"}


def build_rows(tasks: list[dict[str, Any]], order: list[str]) -> dict[str, Any]:
    """Группирует задачи по исполнителю и раскладывает их на диаграмме.

    Задачи с назначенным сроком (pinnedStartOffsetDays задан в normalize_issues,
    когда в Jira указана дата начала/окончания) встают на диаграмме фиксированно
    на эту позицию. Остальные задачи заполняют свободные промежутки между ними
    подряд без зазоров, в порядке приоритета — как раньше.
    """
    by_assignee: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        aid = t.get("assigneeId")
        if not aid:
            continue
        by_assignee.setdefault(aid, []).append(t)

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        pname = item.get("priority")
        r = order.index(pname) if pname in order else len(order) + 10
        return (r, item.get("key") or "")

    rows: list[dict[str, Any]] = []
    for aid, group_raw in by_assignee.items():
        group = sorted(group_raw, key=sort_key)
        display_name = (group[0].get("assigneeName") if group else None) or aid

        pinned_raw = [t for t in group if t.get("pinnedStartOffsetDays") is not None]
        free = [t for t in group if t.get("pinnedStartOffsetDays") is None]

        pinned_raw.sort(key=lambda t: (float(t["pinnedStartOffsetDays"]), *sort_key(t)))

        placed: list[dict[str, Any]] = []
        occupied: list[tuple[float, float]] = []
        cursor = 0.0
        for item in pinned_raw:
            effort = float(item["effortDays"])
            start = max(float(item["pinnedStartOffsetDays"]), cursor)
            placed.append({**_without_pinned_marker(item), "startOffsetDays": round(start, 4), "durationDays": effort})
            end = start + effort
            occupied.append((start, end))
            cursor = end

        for item in free:
            effort = float(item["effortDays"])
            start = 0.0
            while True:
                blocker = next((o for o in occupied if o[0] < start + effort and o[1] > start), None)
                if blocker is None:
                    break
                start = blocker[1]
            placed.append({**_without_pinned_marker(item), "startOffsetDays": round(start, 4), "durationDays": effort})
            occupied.append((start, start + effort))
            occupied.sort(key=lambda o: o[0])

        placed.sort(key=lambda t: (t["startOffsetDays"], *sort_key(t)))
        rows.append({"assigneeId": aid, "assigneeName": display_name, "tasks": placed})

    rows.sort(key=lambda r: (r.get("assigneeName") or "").casefold())
    return {"rows": rows}


def apply_pending_changes(
    tasks: list[dict[str, Any]],
    pending: dict[str, dict[str, Any]],
    settings: dict[str, Any],
) -> list[dict[str, Any]]:
    """Накладывает локальные несохранённые изменения (исполнитель, трудозатраты, даты) на задачи."""
    planning_start, _ = planning_bounds(settings)
    for t in tasks:
        key = t.get("key")
        item = pending.get(key) if (pending and key) else None

        if item and "assigneeId" in item:
            t["assigneeId"] = item["assigneeId"]
            t["assigneeName"] = item.get("assigneeName") or item["assigneeId"]
            t["pendingAssignee"] = True
        else:
            t["pendingAssignee"] = False

        if item and "effortDays" in item:
            t["effortDays"] = float(item["effortDays"])
            t["pendingEffort"] = True
        else:
            t["pendingEffort"] = False

        has_dates = bool(item and ("startDate" in item or "endDate" in item))
        if has_dates:
            if "startDate" in item:
                t["jiraStartDate"] = item["startDate"]
            if "endDate" in item:
                t["jiraEndDate"] = item["endDate"]
            t["pinnedStartOffsetDays"] = compute_pinned_offset(
                planning_start, t.get("jiraStartDate"), t.get("jiraEndDate"), float(t["effortDays"])
            )
        t["pendingDates"] = has_dates
    return tasks
