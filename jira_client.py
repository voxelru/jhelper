"""Минимальный клиент Jira REST API для задач из JQL (Basic Auth: логин + пароль).

Каждая запись в Jira логируется через logger «jhelper.jira»: что за поле, в
какое поле Jira, с каким значением, по какому URL и что ответил сервер. Уровень
подробности задаётся переменной окружения LOG_LEVEL (см. app.py).
"""

from __future__ import annotations

import logging
import os
from typing import Any

import requests

logger = logging.getLogger("jhelper.jira")


class JiraConfigError(RuntimeError):
    pass


def _short(value: Any, limit: int = 300) -> str:
    text = repr(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


def _put_issue_fields(
    base_url: str,
    session: requests.Session,
    issue_key: str,
    payload: dict[str, Any],
    *,
    what: str,
) -> requests.Response:
    """PUT полей задачи с логированием запроса и ответа.

    Сначала пробует REST API v2, при 404 повторяет запрос на v3 (Jira Cloud).
    Возвращает последний ответ — решение об ошибке принимает вызывающий код.
    """
    response: requests.Response | None = None
    for api_version in ("2", "3"):
        url = f"{base_url}/rest/api/{api_version}/issue/{issue_key}"
        logger.info("Jira PUT %s | %s | %s", url, what, _short(payload))
        response = session.put(url, json=payload, timeout=60)
        if response.status_code in (200, 204):
            logger.info("Jira PUT %s | %s | OK (%s)", issue_key, what, response.status_code)
            return response
        if response.status_code == 404 and api_version == "2":
            logger.warning("Jira PUT %s | %s | 404 на api/2, повтор на api/3", issue_key, what)
            continue
        logger.error(
            "Jira PUT %s | %s | %s: %s",
            issue_key,
            what,
            response.status_code,
            response.text[:300],
        )
        return response
    return response


def _require_env(name: str) -> str:
    v = os.environ.get(name, "").strip()
    if not v:
        raise JiraConfigError(f"Переменная окружения {name} не задана")
    return v


def get_jira_session() -> tuple[str, requests.Session]:
    base = _require_env("JIRA_BASE_URL").rstrip("/")
    username = _require_env("JIRA_USERNAME")
    password = _require_env("JIRA_PASSWORD")
    session = requests.Session()
    session.auth = (username, password)
    session.headers["Accept"] = "application/json"
    session.headers["Content-Type"] = "application/json"
    return base, session


def search_issues_jql(
    base_url: str,
    session: requests.Session,
    jql: str,
    *,
    fields: list[str],
    max_results_per_page: int = 100,
) -> list[dict[str, Any]]:
    """Постранично загружает все задачи по JQL."""
    issues: list[dict[str, Any]] = []
    start_at = 0
    while True:
        r = session.get(
            f"{base_url}/rest/api/3/search",
            params={
                "jql": jql,
                "startAt": start_at,
                "maxResults": max_results_per_page,
                "fields": ",".join(fields),
            },
            timeout=120,
        )
        if r.status_code == 404:
            r = session.get(
                f"{base_url}/rest/api/2/search",
                params={
                    "jql": jql,
                    "startAt": start_at,
                    "maxResults": max_results_per_page,
                    "fields": ",".join(fields),
                },
                timeout=120,
            )
        r.raise_for_status()
        data = r.json()
        batch = data.get("issues") or []
        issues.extend(batch)
        total = data.get("total")
        if not batch or (total is not None and len(issues) >= total):
            break
        start_at += len(batch)
    return issues


def update_issue_assignee(
    base_url: str,
    session: requests.Session,
    issue_key: str,
    assignee_id: str,
) -> None:
    """Меняет исполнителя задачи (Jira Server/DC: name; Cloud: accountId)."""
    variants = [
        ("assignee.name", {"fields": {"assignee": {"name": assignee_id}}}),
        ("assignee.accountId", {"fields": {"assignee": {"accountId": assignee_id}}}),
        ("assignee.key", {"fields": {"assignee": {"key": assignee_id}}}),
    ]
    errors: list[str] = []
    for variant, payload in variants:
        r = _put_issue_fields(
            base_url,
            session,
            issue_key,
            payload,
            what=f"исполнитель ({variant}) = {assignee_id}",
        )
        if r.status_code in (200, 204):
            return
        # 400/404 часто значит «не тот идентификатор исполнителя» — пробуем следующий вариант
        if r.status_code in (400, 404):
            errors.append(f"{variant} -> {r.status_code}: {r.text[:300]}")
            continue
        r.raise_for_status()
    detail = " | ".join(errors) if errors else "unknown"
    raise RuntimeError(f"Не удалось назначить исполнителя для {issue_key}: {detail}")


def update_issue_date(
    base_url: str,
    session: requests.Session,
    issue_key: str,
    field_id: str,
    iso_date: str,
) -> None:
    """Записывает дату (начала/окончания) в поле Jira, формат YYYY-MM-DD."""
    payload = {"fields": {field_id: iso_date}}
    r = _put_issue_fields(
        base_url,
        session,
        issue_key,
        payload,
        what=f"дата в поле {field_id} = {iso_date}",
    )
    if r.status_code in (200, 204):
        return
    if r.status_code in (400, 404):
        raise RuntimeError(
            f"Не удалось сохранить дату в поле {field_id} для {issue_key}: "
            f"{r.status_code}: {r.text[:300]}"
        )
    r.raise_for_status()


def update_issue_effort(
    base_url: str,
    session: requests.Session,
    issue_key: str,
    effort_type: str,
    field_id: str | None,
    raw_value: Any,
) -> None:
    """Записывает плановые трудозатраты (ширину прямоугольника) в Jira."""
    if effort_type == "timetracking_original":
        payload = {"fields": {"timetracking": {"originalEstimate": raw_value}}}
    elif effort_type == "timetracking_remaining":
        payload = {"fields": {"timetracking": {"remainingEstimate": raw_value}}}
    elif effort_type in ("number_field", "seconds_field"):
        if not field_id:
            raise RuntimeError("Не задан jira_field_id для трудозатрат (effort.jira_field_id)")
        payload = {"fields": {field_id: raw_value}}
    else:
        raise RuntimeError(f"Сохранение трудозатрат не поддерживается для типа {effort_type}")

    r = _put_issue_fields(
        base_url,
        session,
        issue_key,
        payload,
        what=f"трудозатраты ({effort_type}) = {raw_value}",
    )
    if r.status_code in (200, 204):
        return
    if r.status_code in (400, 404):
        raise RuntimeError(
            f"Не удалось сохранить трудозатраты для {issue_key}: {r.status_code}: {r.text[:300]}"
        )
    r.raise_for_status()
