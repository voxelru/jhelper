"""Загрузка и разбор конфигурации приложения (config/app_settings.yaml).

Модуль не знает про Flask и Jira — только читает и типизирует настройки.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

CONFIG_PATH = Path(__file__).resolve().parent / "config" / "app_settings.yaml"


def load_settings() -> dict[str, Any]:
    with CONFIG_PATH.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


def priorities_cfg(s: dict[str, Any]) -> tuple[list[str], dict[str, str]]:
    """Порядок приоритетов и цвета блоков: priorities.order / priorities.colors."""
    p = s.get("priorities") or {}
    order = list(p.get("order") or [])
    colors = dict(p.get("colors") or {})
    return order, colors


def horizon_cfg(s: dict[str, Any]) -> tuple[str, int]:
    """Горизонт планирования: planning_horizon.kind / .count."""
    ph = s.get("planning_horizon") or {}
    kind = str(ph.get("kind") or "calendar_days").lower().strip()
    count = int(ph.get("count") or 90)
    return kind, max(1, count)


def effort_cfg(s: dict[str, Any]) -> tuple[str, str | None]:
    """Источник трудозатрат: effort.type / effort.jira_field_id."""
    e = s.get("effort") or {}
    t = str(e.get("type") or "timetracking_original").lower().strip()
    fid = e.get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return t, fid


def _field_cfg(s: dict[str, Any], name: str) -> dict[str, Any]:
    fields = s.get("fields") or {}
    cfg = fields.get(name) or {}
    return cfg if isinstance(cfg, dict) else {}


def date_field_cfg(s: dict[str, Any], name: str) -> tuple[str, str | None]:
    """Источник даты: fields.<name>.source (board|jira_field) / .jira_field_id."""
    cfg = _field_cfg(s, name)
    source = str(cfg.get("source") or "board").lower().strip()
    if source not in ("board", "jira_field"):
        source = "board"
    fid = cfg.get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return source, fid


def customer_field_id(s: dict[str, Any]) -> str | None:
    """ID кастомного поля Jira для заказчика: fields.customer.jira_field_id."""
    fid = _field_cfg(s, "customer").get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return fid
