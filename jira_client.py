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
