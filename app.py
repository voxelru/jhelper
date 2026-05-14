from __future__ import annotations

import os
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from flask import Flask, jsonify, render_template

from jira_client import JiraConfigError, get_jira_session, search_issues_jql

load_dotenv()

ROOT = Path(__file__).resolve().parent
CONFIG_PATH = ROOT / "config" / "app_settings.yaml"

app = Flask(__name__)


def load_settings() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def priorities_cfg(s: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    p = s.get("priorities") or {}
    order = list(p.get("order") or s.get("priority_order") or [])
    colors = dict(p.get("colors") or s.get("priority_colors") or {})
    return order, colors


def horizon_cfg(s: dict[str, Any]) -> tuple[str, int]:
    ph = s.get("planning_horizon") or {}
    kind = str(ph.get("kind") or "calendar_days").lower().strip()
    count = int(ph.get("count") or s.get("planning_horizon_calendar_days", 90))
    return kind, max(1, count)


def effort_cfg(s: dict[str, Any]) -> tuple[str, str | None]:
    e = s.get("effort") or {}
    raw = str(e.get("type") or s.get("effort_source") or "timetracking_original").lower().strip()
    legacy_map = {
        "original_estimate": "timetracking_original",
        "remaining_estimate": "timetracking_remaining",
        "story_points": "number_field",
    }
    t = legacy_map.get(raw, raw)
    fid = e.get("jira_field_id") or e.get("field_id") or s.get("story_points_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    if t == "story_points":
        t = "number_field"
    return t, fid


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
    if not seconds or seconds <= 0:
        return float(settings.get("min_effort_working_days", 0.25))
    hours = seconds / 3600.0
    wh = float(settings.get("working_hours_per_day", 8))
    return max(float(settings.get("min_effort_working_days", 0.25)), hours / wh)


def _flatten_field_value(raw: Any) -> Any:
    if raw is None:
        return None
    if isinstance(raw, (int, float)):
        return raw
    if isinstance(raw, str):
        return raw
    if isinstance(raw, dict):
        if "value" in raw and isinstance(raw["value"], (int, float, str)):
            return raw["value"]
        if "amount" in raw:
            return raw.get("amount")
        if raw.get("type") == "number" and "number" in raw:
            return raw.get("number")
    return raw


def effort_days_for_issue(fields: dict[str, Any], effort_type: str, field_id: str | None, settings: dict[str, Any]) -> float:
    min_d = float(settings.get("min_effort_working_days", 0.25))

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


def normalize_issues(raw_issues: list[dict[str, Any]], settings: dict[str, Any]) -> list[dict[str, Any]]:
    effort_type, effort_field_id = effort_cfg(settings)
    order, colors = priorities_cfg(settings)

    tasks: list[dict[str, Any]] = []
    for issue in raw_issues:
        key = issue.get("key")
        fields = issue.get("fields") or {}
        summary = (fields.get("summary") or "").strip() or key
        pr = fields.get("priority") or {}
        priority_name = pr.get("name")
        assignee = fields.get("assignee")
        assignee_id, assignee_name = assignee_identity(assignee if isinstance(assignee, dict) else None)
        if not assignee_id:
            continue

        effort_days = effort_days_for_issue(fields, effort_type, effort_field_id, settings)
        color = colors.get(priority_name) or colors.get("default") or "#78909c"

        tasks.append(
            {
                "key": key,
                "summary": summary,
                "priority": priority_name,
                "assigneeId": assignee_id,
                "assigneeName": assignee_name,
                "effortDays": round(effort_days, 4),
                "color": color,
                "_rank": priority_rank(priority_name, order),
            }
        )

    tasks.sort(key=lambda t: (t["assigneeId"] or "", t["_rank"], t["key"]))
    for t in tasks:
        del t["_rank"]
    return tasks


def build_rows(tasks: list[dict[str, Any]], order: list[str]) -> dict[str, Any]:
    by_assignee: dict[str, list[dict[str, Any]]] = {}
    for t in tasks:
        aid = t.get("assigneeId")
        if not aid:
            continue
        by_assignee.setdefault(aid, []).append(t)

    def sort_key(item: dict[str, Any]) -> tuple[int, str]:
        pname = item.get("priority")
        try:
            r = order.index(pname) if pname in order else len(order) + 10
        except ValueError:
            r = len(order) + 10
        return (r, item.get("key") or "")

    rows: list[dict[str, Any]] = []
    for aid in sorted(by_assignee.keys()):
        group = sorted(by_assignee[aid], key=sort_key)
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
    return {"rows": rows}


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings")
def api_settings():
    s = load_settings()
    order, colors = priorities_cfg(s)
    start, end = planning_bounds(s)
    working_dates = [d.isoformat() for d in iter_working_dates(start, end)]
    etype, efid = effort_cfg(s)
    hk, hc = horizon_cfg(s)
    return jsonify(
        {
            "jql": s.get("jql"),
            "priorities": {"order": order, "colors": colors},
            "priorityColors": colors,
            "priorityOrder": order,
            "pixelsPerWorkingDay": s.get("pixels_per_working_day", 36),
            "planningStart": start.isoformat(),
            "planningEnd": end.isoformat(),
            "planningHorizon": {"kind": hk, "count": hc},
            "workingDates": working_dates,
            "workingDayCount": len(working_dates),
            "effort": {"type": etype, "jiraFieldId": efid},
        }
    )


@app.route("/api/board")
def api_board():
    s = load_settings()
    order, _colors = priorities_cfg(s)
    try:
        base, session = get_jira_session()
    except JiraConfigError as e:
        return jsonify({"error": str(e)}), 400

    effort_type, effort_field_id = effort_cfg(s)
    fields = ["summary", "assignee", "priority", "timetracking"]
    if effort_type in ("number_field", "seconds_field") and effort_field_id:
        fields.append(effort_field_id)

    jql = s.get("jql") or "order by created DESC"
    try:
        issues = search_issues_jql(base, session, jql, fields=fields)
    except Exception as e:
        return jsonify({"error": f"Jira: {e}"}), 502

    tasks = normalize_issues(issues, s)
    board = build_rows(tasks, order)
    start, end = planning_bounds(s)
    board["meta"] = {
        "planningStart": start.isoformat(),
        "planningEnd": end.isoformat(),
        "workingDates": [d.isoformat() for d in iter_working_dates(start, end)],
        "pixelsPerWorkingDay": s.get("pixels_per_working_day", 36),
    }
    return jsonify(board)


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")), debug=True)
