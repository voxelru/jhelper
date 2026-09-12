"""Бизнес-логика доски: нормализация задач Jira, раскладка по строкам,
трудозатраты и даты. Модуль не знает про Flask/HTTP.
"""

from __future__ import annotations

from datetime import date, datetime, timedelta
from typing import Any

from settings import (
    customer_field_id,
    date_field_cfg,
    effort_cfg,
    horizon_cfg,
    priorities_cfg,
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

    for name in ("start_date", "end_date"):
        _, fid = date_field_cfg(settings, name)
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
    start_src, start_fid = date_field_cfg(s, "start_date")
    end_src, end_fid = date_field_cfg(s, "end_date")
    return {
        "customer": {"jiraFieldId": customer_field_id(s)},
        "startDate": {"source": start_src, "jiraFieldId": start_fid},
        "endDate": {"source": end_src, "jiraFieldId": end_fid},
    }


def normalize_issues(raw_issues: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    """Превращает сырые issues Jira в задачи доски (без раскладки по строкам)."""
    effort_type, effort_field_id = effort_cfg(settings)
    order, colors = priorities_cfg(settings)
    cust_fid = customer_field_id(settings)
    start_src, start_fid = date_field_cfg(settings, "start_date")
    end_src, end_fid = date_field_cfg(settings, "end_date")

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

        jira_start = parse_jira_date(fields.get(start_fid)) if start_src == "jira_field" and start_fid else None
        jira_end = parse_jira_date(fields.get(end_fid)) if end_src == "jira_field" and end_fid else None

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
                "priority": priority_name,
                "assigneeId": assignee_id,
                "assigneeName": assignee_name,
                "originalAssigneeId": assignee_id,
                "originalAssigneeName": assignee_name,
                "pendingAssignee": False,
                "effortDays": round(effort_days, 4),
                "color": color,
                "jiraStartDate": jira_start,
                "jiraEndDate": jira_end,
                "missingFields": missing_fields,
                "_rank": priority_rank(priority_name, order),
            }
        )

    tasks.sort(key=lambda t: (t["assigneeId"] or "", t["_rank"], t["key"]))
    for t in tasks:
        del t["_rank"]
    return tasks


def build_rows(tasks: list[dict[str, Any]], order: list[str]) -> dict[str, Any]:
    """Группирует задачи по исполнителю и раскладывает их подряд без зазоров."""
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
        offset = 0.0
        placed: list[dict[str, Any]] = []
        display_name = (group[0].get("assigneeName") if group else None) or aid
        for item in group:
            effort = float(item["effortDays"])
            placed.append(
                {
                    **item,
                    "startOffsetDays": round(offset, 4),
                    "durationDays": effort,
                }
            )
            offset += effort
        rows.append({"assigneeId": aid, "assigneeName": display_name, "tasks": placed})

    rows.sort(key=lambda r: (r.get("assigneeName") or "").casefold())
    return {"rows": rows}


def apply_pending_assignees(
    tasks: list[dict[str, Any]],
    pending: dict[str, dict[str, str]],
) -> list[dict[str, Any]]:
    """Накладывает локальные несохранённые смены исполнителя на задачи."""
    if not pending:
        return tasks
    for t in tasks:
        key = t.get("key")
        if not key or key not in pending:
            t["pendingAssignee"] = False
            continue
        p = pending[key]
        t["assigneeId"] = p["assigneeId"]
        t["assigneeName"] = p["assigneeName"]
        t["pendingAssignee"] = True
    return tasks
