"""Flask-приложение: HTTP-роуты доски задач. Бизнес-логика — в board_logic.py,
настройки — в settings.py, клиент Jira — в jira_client.py."""

from __future__ import annotations

import logging
import os
from datetime import date
from pathlib import Path

from dotenv import load_dotenv
from flask import Flask, jsonify, render_template, request

from board_logic import (
    MAX_WINDOW_WORKING_DAYS,
    apply_pending_changes,
    build_rows,
    collect_jira_fields,
    effort_days_to_jira_raw,
    fields_public_cfg,
    iter_working_dates,
    normalize_issues,
    planning_bounds,
    today_offset_days,
)
from jira_client import (
    JiraConfigError,
    get_jira_session,
    search_issues_jql,
    update_issue_assignee,
    update_issue_date,
    update_issue_effort,
)
from pending_changes import (
    clear_pending,
    pending_list,
    read_pending,
    reset_history,
    undo_last,
    upsert_pending_assignee,
    upsert_pending_placement,
    write_pending,
)
from settings import date_field_cfg, effort_cfg, horizon_cfg, load_settings, priorities_cfg

load_dotenv()

ROOT = Path(__file__).resolve().parent

# Логи записи в Jira: уровень задаётся LOG_LEVEL (по умолчанию INFO), сами
# запросы пишет jira_client через logger «jhelper.jira».
logging.basicConfig(
    level=(os.environ.get("LOG_LEVEL") or "INFO").upper(),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("jhelper.save")

app = Flask(__name__)


def jira_base_url() -> str:
    return (os.environ.get("JIRA_BASE_URL") or "").rstrip("/")


def _query_date(name: str) -> date | None:
    """Дата из query-параметра фильтра (?from=/?to=), ISO YYYY-MM-DD."""
    raw = (request.args.get(name) or "").strip()
    if not raw:
        return None
    try:
        return date.fromisoformat(raw[:10])
    except ValueError:
        return None


def requested_window(s: dict) -> tuple[date, date]:
    """Окно шкалы: выбранное в фильтре по датам или горизонт по умолчанию.

    Ограничений «не раньше сегодня» нет — окно может целиком лежать в прошлом."""
    return planning_bounds(s, _query_date("from"), _query_date("to"))


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/settings")
def api_settings():
    s = load_settings()
    order, colors = priorities_cfg(s)
    start, end = requested_window(s)
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
            "todayOffsetDays": today_offset_days(start),
            "maxWindowWorkingDays": MAX_WINDOW_WORKING_DAYS,
            "workingDates": working_dates,
            "workingDayCount": len(working_dates),
            "effort": {"type": etype, "jiraFieldId": efid},
            "minEffortWorkingDays": s.get("min_effort_working_days", 1),
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

    start, end = requested_window(s)
    today_offset = today_offset_days(start)
    tasks = normalize_issues(issues, s, start)
    pending = read_pending(ROOT)
    tasks = apply_pending_changes(tasks, pending, s, start)
    sprints = sorted({t["sprint"] for t in tasks if t.get("sprint")})
    customers = sorted({t["customer"] for t in tasks if t.get("customer")})
    board = build_rows(tasks, order, today_offset)
    board["meta"] = {
        "planningStart": start.isoformat(),
        "planningEnd": end.isoformat(),
        "todayOffsetDays": today_offset,
        "maxWindowWorkingDays": MAX_WINDOW_WORKING_DAYS,
        "workingDates": [d.isoformat() for d in iter_working_dates(start, end)],
        "pixelsPerWorkingDay": s.get("pixels_per_working_day", 36),
        "fields": fields_public_cfg(s),
        "jiraBaseUrl": jira_base_url(),
        "pendingCount": len(pending),
        "sprints": sprints,
        "customers": customers,
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
        changes = upsert_pending_assignee(
            ROOT,
            key,
            assignee_id,
            assignee_name,
            original_assignee_id=original_id,
        )
    except ValueError as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"changes": pending_list(changes), "count": len(changes)})


@app.route("/api/pending/placement", methods=["POST"])
def api_pending_placement_post():
    """Положение задачи на доске: дата начала, дата окончания и продолжительность.

    Любое перемещение или изменение размера присылает все три параметра сразу —
    частичных обновлений (только даты или только трудозатраты) больше нет.
    """
    data = request.get_json(silent=True) or {}
    key = str(data.get("key") or "").strip()
    effort_days = data.get("effortDays")
    if not key or effort_days is None:
        return jsonify({"error": "Нужны key, startDate, endDate и effortDays"}), 400
    original_effort = data.get("originalEffortDays")
    try:
        changes = upsert_pending_placement(
            ROOT,
            key,
            start_date=data.get("startDate"),
            end_date=data.get("endDate"),
            effort_days=float(effort_days),
            original_start_date=data.get("originalStartDate"),
            original_end_date=data.get("originalEndDate"),
            original_effort_days=float(original_effort) if original_effort is not None else None,
        )
    except (TypeError, ValueError) as e:
        return jsonify({"error": str(e)}), 400
    return jsonify({"changes": pending_list(changes), "count": len(changes)})


@app.route("/api/pending/undo", methods=["POST"])
def api_pending_undo():
    changes = undo_last(ROOT)
    return jsonify({"changes": pending_list(changes), "count": len(changes)})


@app.route("/api/pending/clear", methods=["POST"])
def api_pending_clear():
    clear_pending(ROOT)
    return jsonify({"changes": [], "count": 0})


@app.route("/api/save", methods=["POST"])
def api_save():
    changes = read_pending(ROOT)
    if not changes:
        return jsonify({"saved": 0, "failed": [], "message": "Нет изменений для сохранения"})

    s = load_settings()
    effort_type, effort_field_id = effort_cfg(s)
    start_fid = date_field_cfg(s, "start_date")
    end_fid = date_field_cfg(s, "end_date")

    try:
        base, session = get_jira_session()
    except JiraConfigError as e:
        return jsonify({"error": str(e)}), 400

    saved = 0
    failed: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    remaining: dict[str, dict] = {}

    log.info("Сохранение в Jira: задач с изменениями — %d", len(changes))

    for key, item in changes.items():
        remaining_item: dict = {}

        if "assigneeId" in item:
            try:
                log.info("%s: исполнитель -> %s", key, item["assigneeId"])
                update_issue_assignee(base, session, key, item["assigneeId"])
                saved += 1
            except Exception as e:
                log.error("%s: исполнитель не сохранён: %s", key, e)
                failed.append({"key": key, "field": "assignee", "error": str(e)})
                remaining_item["assigneeId"] = item["assigneeId"]
                remaining_item["assigneeName"] = item.get("assigneeName", item["assigneeId"])

        # Положение задачи — единая запись из трёх параметров: сохраняем их
        # вместе и вместе же возвращаем в pending, если хоть одна часть не ушла.
        has_placement = "effortDays" in item or "startDate" in item or "endDate" in item
        placement_failed = False

        if "effortDays" in item:
            try:
                raw = effort_days_to_jira_raw(item["effortDays"], effort_type, s)
                log.info(
                    "%s: трудозатраты -> %s рабочих дней (в Jira: %r, тип %s)",
                    key,
                    item["effortDays"],
                    raw,
                    effort_type,
                )
                update_issue_effort(base, session, key, effort_type, effort_field_id, raw)
                saved += 1
            except Exception as e:
                log.error("%s: трудозатраты не сохранены: %s", key, e)
                failed.append({"key": key, "field": "effort", "error": str(e)})
                placement_failed = True

        # Дата без настроенного jira_field_id — чисто расчётная: она есть в
        # pending-записи для полноты, но писать её в Jira некуда. Это не ошибка,
        # но и не молчаливая потеря: такие поля попадают в лог и в ответ (skipped).
        for field_name, field_id, cfg_key in (
            ("startDate", start_fid, "fields.start_date.jira_field_id"),
            ("endDate", end_fid, "fields.end_date.jira_field_id"),
        ):
            if field_name not in item:
                continue
            value = item.get(field_name)
            if not value:
                reason = "в изменении нет значения даты"
                log.warning("%s: %s не сохранена — %s", key, field_name, reason)
                skipped.append({"key": key, "field": field_name, "reason": reason})
                continue
            if not field_id:
                reason = f"не задан {cfg_key} в config/app_settings.yaml"
                log.warning("%s: %s (%s) не сохранена — %s", key, field_name, value, reason)
                skipped.append({"key": key, "field": field_name, "reason": reason})
                continue
            try:
                log.info("%s: %s -> %s (поле Jira %s)", key, field_name, value, field_id)
                update_issue_date(base, session, key, field_id, value)
                saved += 1
            except Exception as e:
                log.error("%s: %s не сохранена: %s", key, field_name, e)
                failed.append({"key": key, "field": field_name, "error": str(e)})
                placement_failed = True

        if has_placement and placement_failed:
            for field_name in ("startDate", "endDate", "effortDays"):
                if field_name in item:
                    remaining_item[field_name] = item[field_name]

        if remaining_item:
            remaining[key] = remaining_item

    if remaining:
        write_pending(ROOT, remaining)
    else:
        write_pending(ROOT, {})
    reset_history(ROOT)

    log.info(
        "Сохранение завершено: записано полей — %d, ошибок — %d, пропущено — %d",
        saved,
        len(failed),
        len(skipped),
    )

    status = 200 if not failed else 207
    return (
        jsonify(
            {
                "saved": saved,
                "failed": failed,
                "skipped": skipped,
                "remaining": pending_list(remaining),
                "count": len(remaining),
            }
        ),
        status,
    )


if __name__ == "__main__":
    debug = (os.environ.get("FLASK_DEBUG") or "false").strip().lower() in ("1", "true", "yes")
    app.run(host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "5000")), debug=debug)
