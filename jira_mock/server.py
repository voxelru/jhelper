"""Мок Jira REST API поверх CSV-файла — для отладки jhelper без настоящей Jira.

Каждая строка CSV — задача, каждая колонка — поле. Файл перечитывается на
каждый запрос, поэтому правки в нём видны сразу после обновления доски; все
изменяющие запросы (PUT/POST/DELETE) записывают результат обратно в тот же файл.

Запуск:  python jira_mock/server.py   (затем JIRA_BASE_URL=http://127.0.0.1:8089)
Формат колонок описан в jira_mock/README.md.
"""

from __future__ import annotations

import codecs
import csv
import io
import json
import logging
import os
import re
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from flask import Flask, jsonify, request

from jql import JqlError, run_jql

CSV_PATH = Path(os.environ.get("MOCK_JIRA_CSV") or Path(__file__).resolve().parent / "issues.csv")

# Колонки с особым смыслом; любая другая колонка отдаётся как поле Jira с тем
# же именем (duedate, customfield_10100, …).
STANDARD_COLUMNS = [
    "key",
    "summary",
    "issuetype",
    "status",
    "priority",
    "assignee",
    "assignee_name",
    "original_estimate",
    "remaining_estimate",
    "labels",
]
NAMED_FIELDS = ("issuetype", "status", "priority")
# Необязательная колонка: категория статуса (To Do / In Progress / Done). Если её
# нет или ячейка пуста, категория угадывается по названию статуса.
STATUS_CATEGORY_COLUMN = "status_category"
RESERVED_COLUMNS = (*STANDARD_COLUMNS, STATUS_CATEGORY_COLUMN)

STATUS_CATEGORIES = {"new": "To Do", "indeterminate": "In Progress", "done": "Done"}
_DONE_STATUSES = {
    "done", "closed", "resolved", "готово", "закрыта", "закрыто", "закрыт",
    "выполнена", "выполнено", "решена", "решено", "отменена",
}
_IN_PROGRESS_STATUSES = {
    "in progress", "in review", "review", "testing", "в работе", "на проверке", "ревью", "тестирование",
}

# Как в Jira по умолчанию: день — 8 часов, неделя — 5 дней.
_UNIT_SECONDS = {"w": 5 * 8 * 3600, "d": 8 * 3600, "h": 3600, "m": 60}

app = Flask(__name__)
log = logging.getLogger("jira_mock")
_write_lock = threading.Lock()


@dataclass
class Table:
    columns: list[str]
    rows: list[dict[str, Any]]
    delimiter: str
    encoding: str


def load_table() -> Table:
    raw = CSV_PATH.read_bytes()
    # Excel сохраняет CSV либо в UTF-8 с BOM, либо в cp1251 с «;» — читаем оба
    # варианта и при записи сохраняем тот же формат.
    if raw.startswith(codecs.BOM_UTF8):
        encoding = "utf-8-sig"
    else:
        try:
            raw.decode("utf-8")
            encoding = "utf-8"
        except UnicodeDecodeError:
            encoding = "cp1251"
    text = raw.decode(encoding)
    header = text.splitlines()[0] if text.strip() else ""
    delimiter = ";" if header.count(";") > header.count(",") else ","

    reader = csv.DictReader(io.StringIO(text, newline=""), delimiter=delimiter)
    columns = [(c or "").strip() for c in (reader.fieldnames or [])]
    reader.fieldnames = columns
    rows = list(reader)
    for col in STANDARD_COLUMNS:
        if col not in columns:
            columns.append(col)
    return Table(columns, rows, delimiter, encoding)


def save_table(table: Table) -> None:
    buf = io.StringIO(newline="")
    writer = csv.DictWriter(
        buf,
        fieldnames=table.columns,
        delimiter=table.delimiter,
        lineterminator="\n",
        extrasaction="ignore",
    )
    writer.writeheader()
    writer.writerows(table.rows)
    CSV_PATH.write_bytes(buf.getvalue().encode(table.encoding))


def cell(row: dict[str, Any], column: str) -> str:
    return str(row.get(column) or "").strip()


def find_row(table: Table, issue_key: str) -> dict[str, Any] | None:
    wanted = issue_key.strip().casefold()
    for row in table.rows:
        if cell(row, "key").casefold() == wanted:
            return row
    return None


def parse_duration(text: Any) -> int | None:
    """«3d», «1d 4h», «90m» -> секунды; число без единицы — минуты, как в Jira."""
    text = str(text if text is not None else "").strip().lower().replace(",", ".")
    if not text:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", text):
        return round(float(text) * 60)
    if not re.fullmatch(r"(\s*\d+(\.\d+)?\s*[wdhm])+\s*", text):
        return None
    parts = re.findall(r"(\d+(?:\.\d+)?)\s*([wdhm])", text)
    return round(sum(float(n) * _UNIT_SECONDS[unit] for n, unit in parts))


def format_duration(seconds: int) -> str:
    minutes = round(seconds / 60)
    days, minutes = divmod(minutes, 8 * 60)
    hours, minutes = divmod(minutes, 60)
    parts = [f"{n}{unit}" for n, unit in ((days, "d"), (hours, "h"), (minutes, "m")) if n]
    return " ".join(parts) or "0m"


def cell_to_value(text: str) -> Any:
    """Значение произвольной колонки: пусто -> null, число -> число, JSON -> как есть."""
    if not text:
        return None
    if text[0] in "[{":
        try:
            return json.loads(text)
        except ValueError:
            return text
    if re.fullmatch(r"-?\d+", text):
        return int(text)
    if re.fullmatch(r"-?\d+\.\d+", text):
        return float(text)
    return text


def value_to_cell(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, (dict, list)):
        return json.dumps(value, ensure_ascii=False)
    return str(value)


def status_category(status: str, explicit: str = "") -> dict[str, str]:
    status, explicit = status.strip().casefold(), explicit.strip().casefold()
    for key, name in STATUS_CATEGORIES.items():
        if explicit in (key, name.casefold()):
            return {"key": key, "name": name}
    if status in _DONE_STATUSES:
        key = "done"
    elif status in _IN_PROGRESS_STATUSES:
        key = "indeterminate"
    else:
        key = "new"
    return {"key": key, "name": STATUS_CATEGORIES[key]}


def issue_fields(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    fields: dict[str, Any] = {"summary": cell(row, "summary") or None}

    for name in NAMED_FIELDS:
        value = cell(row, name)
        fields[name] = {"name": value} if value else None
    if fields["status"]:
        fields["status"]["statusCategory"] = status_category(
            cell(row, "status"), cell(row, STATUS_CATEGORY_COLUMN)
        )

    login = cell(row, "assignee")
    if login:
        fields["assignee"] = {
            "name": login,
            "key": login,
            "displayName": cell(row, "assignee_name") or login,
        }
    else:
        fields["assignee"] = None

    timetracking: dict[str, Any] = {}
    for column, prefix in (("original_estimate", "originalEstimate"), ("remaining_estimate", "remainingEstimate")):
        seconds = parse_duration(cell(row, column))
        if seconds is not None:
            timetracking[prefix] = format_duration(seconds)
            timetracking[f"{prefix}Seconds"] = seconds
    fields["timetracking"] = timetracking

    fields["labels"] = [x for x in re.split(r"[\s,;]+", cell(row, "labels")) if x]

    for column in table.columns:
        if column and column not in RESERVED_COLUMNS:
            fields[column] = cell_to_value(cell(row, column))
    return fields


def issue_json(table: Table, row: dict[str, Any]) -> dict[str, Any]:
    key = cell(row, "key")
    return {
        "id": str(10000 + table.rows.index(row)),
        "key": key,
        "self": f"{request.host_url}rest/api/2/issue/{key}",
        "fields": issue_fields(table, row),
    }


def only_fields(issue: dict[str, Any], wanted: set[str] | None) -> dict[str, Any]:
    if wanted is None:
        return issue
    return {**issue, "fields": {name: value for name, value in issue["fields"].items() if name in wanted}}


def wanted_fields(raw: Any = None) -> set[str] | None:
    """Разбор параметра fields (строка через запятую или список); None — отдать все поля."""
    if raw is None:
        raw = request.args.get("fields") or ""
    if isinstance(raw, str):
        raw = raw.split(",")
    names = {str(x).strip() for x in raw if str(x).strip()}
    if not names or any(x.startswith("*") for x in names):
        return None
    return names


def jira_error(status: int, message: str | None = None, errors: dict[str, str] | None = None):
    return jsonify({"errorMessages": [message] if message else [], "errors": errors or {}}), status


def apply_update(table: Table, row: dict[str, Any], fields: dict[str, Any]) -> dict[str, str]:
    """Применяет поля из PUT к строке; возвращает ошибки по полям (как Jira)."""
    errors: dict[str, str] = {}
    updates: dict[str, str] = {}

    for name, value in fields.items():
        if name == "assignee":
            ident = (
                value.get("name") or value.get("accountId") or value.get("key") if isinstance(value, dict) else None
            )
            # null, {"name": null} и «-1» снимают исполнителя, как в Jira.
            if value is None or (isinstance(value, dict) and value and not ident) or ident == "-1":
                updates["assignee"] = updates["assignee_name"] = ""
                continue
            if not ident:
                errors[name] = "expected 'name', 'accountId' or 'key' property"
                continue
            ident = str(ident)
            # Отображаемое имя берём у другой задачи того же исполнителя.
            known = next(
                (cell(r, "assignee_name") for r in table.rows if cell(r, "assignee") == ident and cell(r, "assignee_name")),
                ident,
            )
            updates["assignee"] = ident
            updates["assignee_name"] = known
        elif name == "timetracking":
            if not isinstance(value, dict):
                errors[name] = "data was not an object"
                continue
            for prop, column in (("originalEstimate", "original_estimate"), ("remainingEstimate", "remaining_estimate")):
                if prop not in value:
                    continue
                seconds = parse_duration(value[prop])
                if seconds is None and value[prop] not in (None, ""):
                    errors[name] = f"'{value[prop]}' is an invalid format for a duration."
                    continue
                updates[column] = format_duration(seconds) if seconds is not None else ""
        elif name == "labels":
            if not isinstance(value, list):
                errors[name] = "data was not an array"
                continue
            updates["labels"] = " ".join(str(x) for x in value)
        elif name == "summary":
            updates["summary"] = value_to_cell(value)
        elif name in ("priority", "issuetype"):
            updates[name] = value_to_cell(value.get("name") if isinstance(value, dict) else value)
        elif name in table.columns and name not in RESERVED_COLUMNS:
            updates[name] = value_to_cell(value)
        else:
            errors[name] = f"Field '{name}' cannot be set. It is not on the appropriate screen, or unknown."

    if not errors:
        row.update(updates)
    return errors


def apply_update_operations(table: Table, row: dict[str, Any], update: dict[str, Any]) -> dict[str, str]:
    """Секция «update» из PUT: [{"set": v}] для любого поля, add/remove для меток."""
    fields: dict[str, Any] = {}
    errors: dict[str, str] = {}
    for name, operations in update.items():
        labels = [x for x in re.split(r"[\s,;]+", cell(row, "labels")) if x]
        for operation in operations if isinstance(operations, list) else []:
            verb, value = next(iter(operation.items()), (None, None)) if isinstance(operation, dict) else (None, None)
            if verb == "set":
                fields[name] = value
            elif name == "labels" and verb == "add":
                fields[name] = labels = [*labels, str(value)] if str(value) not in labels else labels
            elif name == "labels" and verb == "remove":
                fields[name] = labels = [x for x in labels if x != str(value)]
            else:
                errors[name] = f"Operation '{verb}' is not supported for field '{name}'"
    return errors or apply_update(table, row, fields)


def distinct(table: Table, column: str) -> list[str]:
    """Уникальные непустые значения колонки в порядке появления в файле."""
    seen: list[str] = []
    for row in table.rows:
        value = cell(row, column)
        if value and value not in seen:
            seen.append(value)
    return seen


def users(table: Table) -> list[dict[str, Any]]:
    result: dict[str, dict[str, Any]] = {}
    for row in table.rows:
        login = cell(row, "assignee")
        if login and login not in result:
            result[login] = {
                "name": login,
                "key": login,
                "displayName": cell(row, "assignee_name") or login,
                "active": True,
            }
    return list(result.values())


def transitions(table: Table) -> list[dict[str, Any]]:
    """Переходы: из любого статуса можно попасть в любой, встречающийся в CSV."""
    return [
        {"id": str(i), "name": name, "to": {"name": name, "statusCategory": status_category(name)}}
        for i, name in enumerate(distinct(table, "status"), start=1)
    ]


def current_user() -> str:
    return (request.authorization.username if request.authorization else None) or "mock"


def run_search(params: dict[str, Any]):
    table = load_table()
    issues = [issue_json(table, row) for row in table.rows if cell(row, "key")]
    not_fields = ("key", "assignee_name", "original_estimate", "remaining_estimate", STATUS_CATEGORY_COLUMN)
    field_ids = [c for c in table.columns if c and c not in not_fields]
    jql = str(params.get("jql") or "")
    try:
        found = run_jql(jql, issues, field_ids, current_user())
    except JqlError as e:
        log.warning("search: JQL отклонён (%r): %s", jql, e)
        return jira_error(400, str(e))
    log.info("search: %r -> %d из %d задач", jql, len(found), len(issues))

    try:
        start_at = max(0, int(params.get("startAt") or 0))
        max_results = max(0, int(params["maxResults"])) if params.get("maxResults") is not None else 50
    except (TypeError, ValueError):
        return jira_error(400, "startAt и maxResults должны быть числами")
    wanted = wanted_fields(params.get("fields") or "")
    return jsonify(
        {
            "startAt": start_at,
            "maxResults": max_results,
            "total": len(found),
            "issues": [only_fields(issue, wanted) for issue in found[start_at : start_at + max_results]],
        }
    )


@app.get("/rest/api/<version>/search")
def search(version: str):
    return run_search(request.args.to_dict())


@app.post("/rest/api/<version>/search")
def search_post(version: str):
    payload = request.get_json(silent=True)
    return run_search(payload if isinstance(payload, dict) else {})


@app.post("/rest/api/<version>/issue")
def create_issue(version: str):
    payload = request.get_json(silent=True)
    fields = dict(payload.get("fields") or {}) if isinstance(payload, dict) else None
    if not isinstance(fields, dict):
        return jira_error(400, "Тело запроса должно быть JSON вида {\"fields\": {...}}")
    project = fields.pop("project", None)
    project_key = str((project.get("key") if isinstance(project, dict) else project) or "").strip()
    errors = {}
    if not project_key:
        errors["project"] = "project is required"
    if not str(fields.get("summary") or "").strip():
        errors["summary"] = "You must specify a summary of the issue."
    if errors:
        return jira_error(400, errors=errors)

    with _write_lock:
        table = load_table()
        numbers = [
            int(m.group(1))
            for m in (re.fullmatch(rf"{re.escape(project_key)}-(\d+)", cell(r, "key"), re.I) for r in table.rows)
            if m
        ]
        row: dict[str, Any] = {
            "key": f"{project_key}-{max(numbers, default=0) + 1}",
            "status": next(iter(distinct(table, "status")), "To Do"),
        }
        errors = apply_update(table, row, fields)
        if errors:
            log.warning("POST issue отклонён: %s", errors)
            return jira_error(400, errors=errors)
        table.rows.append(row)
        save_table(table)
        created = issue_json(table, row)
    log.info("POST issue: создана %s: %s", created["key"], json.dumps(fields, ensure_ascii=False))
    return jsonify({"id": created["id"], "key": created["key"], "self": created["self"]}), 201


@app.get("/rest/api/<version>/issue/<issue_key>")
def get_issue(version: str, issue_key: str):
    table = load_table()
    row = find_row(table, issue_key)
    if row is None:
        return jira_error(404, "Issue Does Not Exist")
    return jsonify(only_fields(issue_json(table, row), wanted_fields()))


def update_issue(issue_key: str, fields: dict[str, Any], update: dict[str, Any] | None = None):
    """Общая запись полей в CSV для PUT задачи и PUT исполнителя."""
    with _write_lock:
        table = load_table()
        row = find_row(table, issue_key)
        if row is None:
            return jira_error(404, "Issue Does Not Exist")
        errors = apply_update(table, row, fields)
        if not errors and update:
            errors = apply_update_operations(table, row, update)
        if errors:
            log.warning("PUT %s отклонён: %s", issue_key, errors)
            return jira_error(400, errors=errors)
        save_table(table)
    log.info(
        "PUT %s записан в %s: %s",
        issue_key,
        CSV_PATH.name,
        json.dumps({**fields, **(update or {})}, ensure_ascii=False),
    )
    return "", 204


@app.put("/rest/api/<version>/issue/<issue_key>")
def put_issue(version: str, issue_key: str):
    payload = request.get_json(silent=True)
    fields = payload.get("fields", {}) if isinstance(payload, dict) else None
    update = payload.get("update", {}) if isinstance(payload, dict) else None
    if not isinstance(fields, dict) or not isinstance(update, dict) or not (fields or update):
        return jira_error(400, "Тело запроса должно быть JSON вида {\"fields\": {...}} или {\"update\": {...}}")
    return update_issue(issue_key, fields, update)


@app.put("/rest/api/<version>/issue/<issue_key>/assignee")
def put_assignee(version: str, issue_key: str):
    payload = request.get_json(silent=True)
    if not isinstance(payload, dict):
        return jira_error(400, "Тело запроса должно быть JSON вида {\"name\": \"login\"}")
    return update_issue(issue_key, {"assignee": payload or None})


@app.delete("/rest/api/<version>/issue/<issue_key>")
def delete_issue(version: str, issue_key: str):
    with _write_lock:
        table = load_table()
        row = find_row(table, issue_key)
        if row is None:
            return jira_error(404, "Issue Does Not Exist")
        table.rows.remove(row)
        save_table(table)
    log.info("DELETE %s: строка удалена из %s", issue_key, CSV_PATH.name)
    return "", 204


@app.get("/rest/api/<version>/issue/<issue_key>/transitions")
def get_transitions(version: str, issue_key: str):
    table = load_table()
    if find_row(table, issue_key) is None:
        return jira_error(404, "Issue Does Not Exist")
    return jsonify({"transitions": transitions(table)})


@app.post("/rest/api/<version>/issue/<issue_key>/transitions")
def do_transition(version: str, issue_key: str):
    payload = request.get_json(silent=True)
    transition = payload.get("transition") if isinstance(payload, dict) else None
    wanted_id = str(transition.get("id") or "") if isinstance(transition, dict) else ""
    with _write_lock:
        table = load_table()
        row = find_row(table, issue_key)
        if row is None:
            return jira_error(404, "Issue Does Not Exist")
        target = next((t for t in transitions(table) if t["id"] == wanted_id), None)
        if target is None:
            return jira_error(400, f"Transition id '{wanted_id}' is not valid for this issue.")
        row["status"] = target["name"]
        if STATUS_CATEGORY_COLUMN in table.columns:
            row[STATUS_CATEGORY_COLUMN] = ""
        save_table(table)
    log.info("POST %s: статус -> %s", issue_key, target["name"])
    return "", 204


@app.get("/rest/api/<version>/myself")
def myself(version: str):
    login = current_user()
    known = next((u for u in users(load_table()) if u["name"] == login), None)
    return jsonify(known or {"name": login, "key": login, "displayName": login, "active": True})


@app.get("/rest/api/<version>/user/search")
@app.get("/rest/api/<version>/user/assignable/search")
def user_search(version: str):
    query = (request.args.get("username") or request.args.get("query") or "").strip().casefold()
    found = [u for u in users(load_table()) if query in u["name"].casefold() or query in u["displayName"].casefold()]
    return jsonify(found)


@app.get("/rest/api/<version>/field")
def list_fields(version: str):
    """Список полей — удобно подсматривать ID для config/app_settings.yaml."""
    table = load_table()
    system = ["summary", "issuetype", "status", "priority", "assignee", "timetracking", "labels"]
    custom = [c for c in table.columns if c and c not in RESERVED_COLUMNS]
    return jsonify(
        [{"id": name, "name": name, "custom": name.startswith("customfield_")} for name in (*system, *custom)]
    )


@app.get("/rest/api/<version>/<any(priority, status, issuetype):column>")
def list_named(version: str, column: str):
    table = load_table()
    items = [{"id": str(i), "name": name} for i, name in enumerate(distinct(table, column), start=1)]
    if column == "status":
        for item in items:
            item["statusCategory"] = status_category(item["name"])
    return jsonify(items)


@app.get("/rest/api/<version>/project")
def list_projects(version: str):
    keys: list[str] = []
    for row in load_table().rows:
        project_key = cell(row, "key").rsplit("-", 1)[0]
        if project_key and project_key not in keys:
            keys.append(project_key)
    return jsonify([{"id": str(i), "key": key, "name": key} for i, key in enumerate(keys, start=1)])


@app.get("/rest/api/<version>/serverInfo")
def server_info(version: str):
    return jsonify(
        {
            "baseUrl": request.host_url.rstrip("/"),
            "version": "9.12.0",
            "deploymentType": "Server",
            "serverTitle": f"Jira mock ({CSV_PATH.name})",
        }
    )


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    log.info("Мок Jira, данные: %s", CSV_PATH)
    app.run(
        host=os.environ.get("MOCK_JIRA_HOST", "127.0.0.1"),
        port=int(os.environ.get("MOCK_JIRA_PORT", "8089")),
    )
