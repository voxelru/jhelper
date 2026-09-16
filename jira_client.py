"""Минимальный клиент Jira REST API для задач из JQL (Basic Auth: логин + пароль)."""

from __future__ import annotations

import os
from typing import Any

import requests


class JiraConfigError(RuntimeError):
    pass


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
    issue_url = f"{base_url}/rest/api/2/issue/{issue_key}"
    payloads = [
        {"fields": {"assignee": {"name": assignee_id}}},
        {"fields": {"assignee": {"accountId": assignee_id}}},
        {"fields": {"assignee": {"key": assignee_id}}},
    ]
    errors: list[str] = []
    for payload in payloads:
        r = session.put(issue_url, json=payload, timeout=60)
        if r.status_code in (200, 204):
            return
        if r.status_code == 404:
            r3 = session.put(
                f"{base_url}/rest/api/3/issue/{issue_key}",
                json=payload,
                timeout=60,
            )
            if r3.status_code in (200, 204):
                return
            errors.append(f"{r3.status_code}: {r3.text[:300]}")
            continue
        # 400 часто значит «не тот идентификатор исполнителя» — пробуем следующий вариант
        if r.status_code == 400:
            errors.append(f"{r.status_code}: {r.text[:300]}")
            continue
        r.raise_for_status()
    detail = " | ".join(errors) if errors else "unknown"
    raise RuntimeError(f"Не удалось назначить исполнителя для {issue_key}: {detail}")


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

    issue_url = f"{base_url}/rest/api/2/issue/{issue_key}"
    r = session.put(issue_url, json=payload, timeout=60)
    if r.status_code in (200, 204):
        return
    if r.status_code == 404:
        r3 = session.put(f"{base_url}/rest/api/3/issue/{issue_key}", json=payload, timeout=60)
        if r3.status_code in (200, 204):
            return
        raise RuntimeError(f"Не удалось сохранить трудозатраты для {issue_key}: {r3.status_code}: {r3.text[:300]}")
    if r.status_code == 400:
        raise RuntimeError(f"Не удалось сохранить трудозатраты для {issue_key}: {r.status_code}: {r.text[:300]}")
    r.raise_for_status()
