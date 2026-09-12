"""Flask-приложение: HTTP-роуты доски задач. Бизнес-логика — в board_logic.py,
настройки — в settings.py, клиент Jira — в jira_client.py."""

from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from board_logic import (
    apply_pending_assignees,
    build_rows,
    collect_jira_fields,
    fields_public_cfg,
    iter_working_dates,
    normalize_issues,
    planning_bounds,
)
from jira_client import JiraConfigError, get_jira_session, search_issues_jql, update_issue_assignee
from pending_changes import clear_pending, pending_list, read_pending, upsert_pending, write_pending
from settings import effort_cfg, horizon_cfg, load_settings, priorities_cfg

load_dotenv()

ROOT = Path(__file__).resolve().parent

app = Flask(__name__)


def jira_base_url() -> str:
    return (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")


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
            "pixelsPerWorkingDay": s.get("pixels_per_working_day", 36),
            "planningStart": start.isoformat(),
            "planningEnd": end.isoformat(),
            "planningHorizon": {"kind": hk, "count": hc},
            "workingDates": working_dates,
            "workingDayCount": len(working_dates),
            "effort": {"type": etype, "jiraFieldId": efid},
            "fields": fields_public_cfg(s),
            "jiraBaseUrl": jira_base_url(),
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

    jql = s.get("jql") or "order by created DESC"
    try:
        issues = search_issues_jql(base, session, jql, fields=collect_jira_fields(s))
    except Exception as e:
        return jsonify({"error": f"Jira: {e}"}), 502

    tasks = normalize_issues(issues, s)
    pending = read_pending(ROOT)
    tasks = apply_pending_assignees(tasks, pending)
    board = build_rows(tasks, order)
    start, end = planning_bounds(s)
    board["meta"] = {
        "planningStart": start.isoformat(),
        "planningEnd": end.isoformat(),
        "workingDates": [d.isoformat() for d in iter_working_dates(start, end)],
        "pixelsPerWorkingDay": s.get("pixels_per_working_day", 36),
        "fields": fields_public_cfg(s),
        "jiraBaseUrl": jira_base_url(),
        "pendingCount": len(pending),
    }
    return jsonify(board)


@app.route("/api/pending", methods=["GET"])
def api_pending_get():
    changes = read_pending(ROOT)
    return jsonify({"changes": pending_list(changes), "count": len(changes)})


@app.route("/api/pending", methods=["POST"])
def api_pending_post():
    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or "").strip()
    assignee_id = str(data.get("assigneeId") or "").strip()
    assignee_name = str(data.get("assigneeName") or assignee_id).strip()
    original = data.get("originalAssigneeId")
    original_id = str(original).strip() if original is not None else None
    if not key or not assignee_id:
        return jsonify({"error": "Нужны key и assigneeId"}), 400
    try:
        changes = upsert_pending(
            ROOT,
            key,
            assignee_id,
            assignee_name,
            original_assignee_id=original_id,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"changes": pending_list(changes), "count": len(changes)})


@app.route("/api/save", methods=["POST"])
def api_save():
    changes = read_pending(ROOT)
    if not changes:
        return jsonify({"saved": 0, "failed": [], "message": "Нет изменений для сохранения"})

    try:
        base, session = get_jira_session()
    except JiraConfigError as e:
        return jsonify({"error": str(e)}), 400

    saved = 0
    failed: list[dict[str, str]] = []
    remaining = dict(changes)

    for key, item in list(changes.items()):
        try:
            update_issue_assignee(base, session, key, item["assigneeId"])
            remaining.pop(key, None)
            saved += 1
        except Exception as e:
            failed.append({"key": key, "error": str(e)})

    if remaining:
        write_pending(ROOT, remaining)
    else:
        clear_pending(ROOT)

    status = 200 if not failed else 207
    return (
        jsonify(
            {
                "saved": saved,
                "failed": failed,
                "remaining": pending_list(remaining),
                "count": len(remaining),
            }
        ),
        status,
    )


if __name__ == "__main__":
    debug = (os.environ.get("FLASK_DEBUG") or "false").strip().lower() in ("1", "true", "yes")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=debug)
