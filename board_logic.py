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
    labels_field_id,
    priorities_cfg,
    sprint_field_id,
)

# Системные поля Jira, которые нужны всегда вне зависимости от настроек.
BASE_JIRA_FIELDS = ["summary", "assignee", "priority", "status", "timetracking"]


# Предохранитель на случай, когда в фильтре выбран очень широкий интервал:
# больше этого числа рабочих дней на шкалу не строим.
MAX_WINDOW_WORKING_DAYS = 400


def _horizon_end(start: date, kind: str, count: int) -> date:
    """Конец горизонта планирования, отсчитанного от start."""
    if kind == "working_days":
        collected: list[date] = []
        d = start
        while len(collected) < count:
            if d.weekday() < 5:
                collected.append(d)
            d += timedelta(days=1)
        return collected[-1] if collected else start
    return start + timedelta(days=count - 1)


def _cap_end(start: date, end: date) -> date:
    """Подрезает конец окна, если в нём больше MAX_WINDOW_WORKING_DAYS рабочих дней."""
    count = 0
    d = start
    while d <= end:
        if d.weekday() < 5:
            count += 1
            if count >= MAX_WINDOW_WORKING_DAYS:
                return d
        d += timedelta(days=1)
    return end


def planning_bounds(
    settings: dict[str, Any],
    start: date | None = None,
    end: date | None = None,
) -> tuple[date, date]:
    """Окно шкалы времени.

    По умолчанию — от сегодняшнего дня на горизонт планирования из настроек.
    Фильтр по датам на странице может задать любой другой интервал, в том
    числе целиком в прошлом: тогда шкала строится по нему (с ограничением
    MAX_WINDOW_WORKING_DAYS на ширину окна).
    """
    kind, count = horizon_cfg(settings)
    today = date.today()
    if start and end and end < start:
        start, end = end, start
    if start is None and end is None:
        start = today
        end = _horizon_end(start, kind, count)
    elif end is None:
        end = _horizon_end(start, kind, count)
    elif start is None:
        start = min(today, end)
    return start, _cap_end(start, end)


def iter_working_dates(start: date, end: date) -> list[date]:
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += timedelta(days=1)
    return out


def today_offset_days(planning_start: date) -> int:
    """Смещение сегодняшнего дня от начала окна в рабочих днях.

    От него стартует очередь незапланированных задач: работу без дат нет
    смысла раскладывать задним числом. Значение отрицательное, если окно
    начинается позже сегодняшнего дня — тогда очередь стоит левее окна, и
    доска покажет только ту её часть, что дотягивается до выбранных дат."""
    return working_days_between(planning_start, date.today())


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


def compute_date_range(
    planning_start: date,
    jira_start: str | None,
    jira_end: str | None,
) -> tuple[float, float] | None:
    """(startOffsetDays, durationDays) — точный диапазон по датам начала/окончания
    в Jira (в рабочих днях от planning_start), или None, если задана не обе даты."""
    if not jira_start or not jira_end:
        return None
    # Смещение может быть отрицательным (задача началась раньше окна) — такую
    # задачу доска покажет обрезанной по левому краю.
    start_offset = float(working_days_between(planning_start, date.fromisoformat(jira_start)))
    end_offset_excl = float(working_days_between(planning_start, date.fromisoformat(jira_end)) + 1)
    duration = max(0.25, end_offset_excl - start_offset)
    return start_offset, duration


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


def parse_labels(raw: Any) -> list[str]:
    """Метки задачи списком строк.

    Системное поле Jira `labels` приходит списком строк, кастомные поля —
    списком объектов или строкой через запятую; поддерживаем все варианты.
    """
    if raw is None:
        return []
    if isinstance(raw, list):
        items: list[Any] = list(raw)
    elif isinstance(raw, str):
        items = re.split(r"[,;]", raw)
    else:
        items = [raw]

    out: list[str] = []
    for item in items:
        text = format_display_value(item)
        text = text.strip() if text else None
        if text and text not in out:
            out.append(text)
    return out


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

    labels_fid = labels_field_id(settings)
    if labels_fid:
        fields.append(labels_fid)

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
        "labels": {"jiraFieldId": labels_field_id(s)},
        "startDate": {"jiraFieldId": date_field_cfg(s, "start_date")},
        "endDate": {"jiraFieldId": date_field_cfg(s, "end_date")},
    }


def normalize_issues(
    raw_issues: list[dict[str, Any]],
    settings: dict[str, Any],
    planning_start: date | None = None,
) -> list[dict[str, Any]]:
    """Превращает сырые issues Jira в задачи доски (без раскладки по строкам).

    planning_start — начало окна шкалы, от которого считаются смещения задач
    (по умолчанию — начало горизонта планирования)."""
    effort_type, effort_field_id = effort_cfg(settings)
    order, colors = priorities_cfg(settings)
    cust_fid = customer_field_id(settings)
    sprint_fid = sprint_field_id(settings)
    labels_fid = labels_field_id(settings)
    start_fid = date_field_cfg(settings, "start_date")
    end_fid = date_field_cfg(settings, "end_date")
    if planning_start is None:
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
        has_effort = has_jira_effort(fields, effort_type, effort_field_id)
        color = colors.get(priority_name) or colors.get("default") or "#78909c"
        customer = format_display_value(fields.get(cust_fid)) if cust_fid else None
        sprint = parse_sprint_value(fields.get(sprint_fid)) if sprint_fid else None
        labels = parse_labels(fields.get(labels_fid)) if labels_fid else []

        jira_start = parse_jira_date(fields.get(start_fid)) if start_fid else None
        jira_end = parse_jira_date(fields.get(end_fid)) if end_fid else None

        # Задача «полностью заполнена» (дата начала + дата окончания + трудозатраты
        # заданы в Jira) — занимает на диаграмме строго свой диапазон дат.
        # Иначе — уходит в конец очереди сотрудника и заполняет свободные дни
        # по трудозатратам (старая последовательная раскладка).
        is_complete = bool(jira_start) and bool(jira_end) and has_effort
        date_range = compute_date_range(planning_start, jira_start, jira_end) if is_complete else None

        missing_fields: list[str] = []
        if start_fid and not jira_start:
            missing_fields.append("дата начала")
        if end_fid and not jira_end:
            missing_fields.append("дата окончания")
        if not has_effort:
            missing_fields.append("трудозатраты")

        tasks.append(
            {
                "key": key,
                "summary": summary,
                "status": status_name,
                "customer": customer,
                "sprint": sprint,
                "labels": labels,
                "priority": priority_name,
                "assigneeId": assignee_id,
                "assigneeName": assignee_name,
                "originalAssigneeId": assignee_id,
                "originalAssigneeName": assignee_name,
                "pendingAssignee": False,
                "pendingPlacement": False,
                "effortDays": round(effort_days, 4),
                "originalEffortDays": round(effort_days, 4),
                "hasEffort": has_effort,
                "color": color,
                "jiraStartDate": jira_start,
                "jiraEndDate": jira_end,
                "originalJiraStartDate": jira_start,
                "originalJiraEndDate": jira_end,
                "isComplete": is_complete,
                "dateRangeStartOffsetDays": round(date_range[0], 4) if date_range else None,
                "dateRangeDurationDays": round(date_range[1], 4) if date_range else None,
                "missingFields": missing_fields,
                "_rank": priority_rank(priority_name, order),
            }
        )

    tasks.sort(key=lambda t: (t["assigneeId"] or "", t["_rank"], t["key"]))
    for t in tasks:
        del t["_rank"]
    return tasks


def build_rows(
    tasks: list[dict[str, Any]],
    order: list[str],
    today_offset: float = 0.0,
) -> dict[str, Any]:
    """Группирует задачи по исполнителю и раскладывает их на диаграмме.

    Без автоматической расстановки по свободным промежуткам: полностью
    заполненные задачи (isComplete — есть дата начала, окончания и
    трудозатраты) встают строго на свой диапазон дат. Остальные задачи —
    в очередь после самой поздней из них, подряд без зазоров, в порядке
    приоритета, как в самой первой версии доски. Так между полностью
    заполненными задачами намеренно остаются пустые промежутки, куда
    можно вручную перетащить любую задачу.

    today_offset — смещение сегодняшнего дня от начала окна: очередь
    незапланированных задач начинается не раньше него, чтобы при просмотре
    прошлого они не раскладывались задним числом.
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
        display_name = (group_raw[0].get("assigneeName") if group_raw else None) or aid

        complete = [t for t in group_raw if t.get("isComplete")]
        incomplete = sorted((t for t in group_raw if not t.get("isComplete")), key=sort_key)

        complete.sort(key=lambda t: (float(t["dateRangeStartOffsetDays"]), *sort_key(t)))

        placed: list[dict[str, Any]] = []
        cursor: float | None = None
        for item in complete:
            duration = float(item["dateRangeDurationDays"])
            start = float(item["dateRangeStartOffsetDays"])
            if cursor is not None:
                start = max(start, cursor)
            placed.append({**item, "startOffsetDays": round(start, 4), "durationDays": round(duration, 4)})
            cursor = start + duration

        cursor = float(today_offset) if cursor is None else max(cursor, float(today_offset))
        for item in incomplete:
            duration = float(item["effortDays"])
            placed.append({**item, "startOffsetDays": round(cursor, 4), "durationDays": round(duration, 4)})
            cursor += duration

        rows.append({"assigneeId": aid, "assigneeName": display_name, "tasks": placed})

    rows.sort(key=lambda r: (r.get("assigneeName") or "").casefold())
    return {"rows": rows}


def apply_pending_changes(
    tasks: list[dict[str, Any]],
    pending: dict[str, dict[str, Any]],
    settings: dict[str, Any],
    planning_start: date | None = None,
) -> list[dict[str, Any]]:
    """Накладывает локальные несохранённые изменения на задачи.

    Это исполнитель и положение задачи (дата начала + дата окончания +
    продолжительность), которое всегда лежит в pending целиком."""
    if planning_start is None:
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

        # Положение задачи лежит в pending одной записью из трёх параметров:
        # дата начала, дата окончания и продолжительность (effortDays).
        has_placement = bool(
            item and ("startDate" in item or "endDate" in item or "effortDays" in item)
        )
        if has_placement:
            if item.get("effortDays") is not None:
                t["effortDays"] = float(item["effortDays"])
            if item.get("startDate"):
                t["jiraStartDate"] = item["startDate"]
            if item.get("endDate"):
                t["jiraEndDate"] = item["endDate"]
        t["pendingPlacement"] = has_placement

        # Явное перетаскивание/растягивание задачи само по себе даёт достаточно
        # данных о её длительности — не требуем отдельно заполненных
        # «трудозатрат» в Jira, чтобы задача считалась полностью заполненной.
        has_effort_now = bool(t.get("hasEffort")) or has_placement
        t["isComplete"] = bool(t.get("jiraStartDate")) and bool(t.get("jiraEndDate")) and has_effort_now
        date_range = (
            compute_date_range(planning_start, t.get("jiraStartDate"), t.get("jiraEndDate"))
            if t["isComplete"]
            else None
        )
        t["dateRangeStartOffsetDays"] = round(date_range[0], 4) if date_range else None
        t["dateRangeDurationDays"] = round(date_range[1], 4) if date_range else None
    return tasks
