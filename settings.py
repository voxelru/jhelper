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


def issue_type_overrides(s: dict[str, Any]) -> dict[str, Any]:
    """Секция issue_types: переопределения полей для отдельных типов задач."""
    raw = s.get("issue_types") or {}
    return raw if isinstance(raw, dict) else {}


def _merge_field_cfg(base: Any, override: Any) -> Any:
    if isinstance(base, dict) and isinstance(override, dict):
        return {**base, **override}
    return override


def settings_for_issue_type(
    s: dict[str, Any],
    issue_type_name: str | None = None,
    issue_type_id: str | None = None,
) -> dict[str, Any]:
    """Настройки с учётом переопределений для конкретного типа задачи.

    Ключом в issue_types может быть название типа («Задача», «Epic»; регистр и
    пробелы по краям не важны) или его id. В переопределении достаточно указать
    только отличающиеся секции effort/fields — остальное берётся из корневых
    настроек, поэтому дальше с результатом работают те же effort_cfg(),
    date_field_cfg() и прочие функции модуля.
    """
    overrides = issue_type_overrides(s)
    if not overrides:
        return s

    wanted_name = str(issue_type_name).strip().casefold() if issue_type_name else None
    wanted_id = str(issue_type_id).strip() if issue_type_id else None

    match: dict[str, Any] | None = None
    for key, cfg in overrides.items():
        if not isinstance(cfg, dict):
            continue
        key_text = str(key).strip()
        if wanted_name and key_text.casefold() == wanted_name:
            match = cfg
            break
        if wanted_id and key_text == wanted_id:
            match = cfg
            break
    if not match:
        return s

    merged = dict(s)
    if isinstance(match.get("effort"), dict):
        merged["effort"] = {**(s.get("effort") or {}), **match["effort"]}
    if isinstance(match.get("fields"), dict):
        base_fields = s.get("fields") or {}
        fields = {k: (dict(v) if isinstance(v, dict) else v) for k, v in base_fields.items()}
        for name, cfg in match["fields"].items():
            fields[name] = _merge_field_cfg(fields.get(name), cfg)
        merged["fields"] = fields
    return merged


def _field_cfg(s: dict[str, Any], name: str) -> dict[str, Any]:
    fields = s.get("fields") or {}
    cfg = fields.get(name) or {}
    return cfg if isinstance(cfg, dict) else {}


def date_field_cfg(s: dict[str, Any], name: str) -> str | None:
    """ID поля Jira для даты (начала/окончания): fields.<name>.jira_field_id.

    Если задан — эта дата читается из Jira и пишется обратно при сохранении.
    Любое перемещение/изменение размера в любом случае записывает в
    pending_changes.json все три параметра положения (дата начала, дата
    окончания, продолжительность); дата без jira_field_id просто пропускается
    при отправке в Jira."""
    fid = _field_cfg(s, name).get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return fid


def customer_field_id(s: dict[str, Any]) -> str | None:
    """ID кастомного поля Jira для заказчика: fields.customer.jira_field_id."""
    fid = _field_cfg(s, "customer").get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return fid


def labels_field_id(s: dict[str, Any]) -> str | None:
    """ID поля Jira с метками: fields.labels.jira_field_id.

    Обычно это системное поле `labels`, но можно указать любое кастомное —
    значение используется для выпадающего фильтра по меткам."""
    fid = _field_cfg(s, "labels").get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return fid


def sprint_field_id(s: dict[str, Any]) -> str | None:
    """ID поля Jira со спринтом: fields.sprint.jira_field_id."""
    fid = _field_cfg(s, "sprint").get("jira_field_id")
    if fid is not None:
        fid = str(fid).strip() or None
    return fid
