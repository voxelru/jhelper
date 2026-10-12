"""Подмножество JQL для мока Jira: фильтрация и сортировка задач из CSV.

Поддерживается:
  field = v, !=, ~, !~, >, >=, <, <=, [NOT] IN (a, b), IS [NOT] EMPTY
  AND / OR / NOT, скобки, ORDER BY f1 [ASC|DESC], f2 ...
  currentUser(), now(), EMPTY / NULL

Поля: key, project, summary, assignee, status, statusCategory, priority,
issuetype (type), labels и любая колонка CSV по её имени или как cf[10100].
Всё остальное (история, WAS/CHANGED, прочие функции) даёт JqlError — сервер
отвечает на него 400, как настоящая Jira на неверный запрос.
"""

from __future__ import annotations

import operator
import re
from datetime import datetime
from typing import Any, Callable

PRIORITY_ORDER = ["lowest", "low", "medium", "high", "highest"]

_TOKEN = re.compile(
    r"""\s*(?:
        (?P<str>"(?:[^"\\]|\\.)*"|'(?:[^'\\]|\\.)*')
      | (?P<op>!=|!~|>=|<=|=|~|>|<)
      | (?P<punct>[(),])
      | (?P<word>[^\s()=!~<>,'"]+)
    )""",
    re.X,
)
_ALIASES = {"issuekey": "key", "issue": "key", "id": "key", "type": "issuetype", "text": "summary"}
_VIRTUAL_FIELDS = ("key", "project", "statuscategory")
_RELATIONAL = {">": operator.gt, ">=": operator.ge, "<": operator.lt, "<=": operator.le}

Issue = dict[str, Any]
Predicate = Callable[[Issue], bool]


class JqlError(ValueError):
    pass


def _flatten(raw: Any) -> list[Any]:
    if raw is None or raw == "":
        return []
    if isinstance(raw, list):
        return [v for item in raw for v in _flatten(item)]
    if isinstance(raw, dict):
        return [raw[k] for k in ("name", "displayName", "value", "key") if raw.get(k)]
    return [raw]


def _values(issue: Issue, field: str) -> list[Any]:
    key = str(issue.get("key") or "")
    fields = issue.get("fields") or {}
    if field == "key":
        return [key]
    if field == "project":
        return [key.rsplit("-", 1)[0]]
    if field == "statuscategory":
        return _flatten((fields.get("status") or {}).get("statusCategory"))
    return _flatten(fields.get(field))


def _norm(value: Any) -> str:
    return str(value).strip().casefold()


def _order_key(field: str, value: Any) -> tuple:
    """Ключ сравнения: числа — как числа, приоритеты — по рангу, ключи — по номеру."""
    text = _norm(value)
    if field == "priority":
        return (0, float(PRIORITY_ORDER.index(text) + 1 if text in PRIORITY_ORDER else 0))
    if field == "key":
        m = re.fullmatch(r"(.+)-(\d+)", text)
        if m:
            return (1, m.group(1), int(m.group(2)))
    if re.fullmatch(r"-?\d+(\.\d+)?", text):
        return (0, float(text))
    return (1, text)


class _Parser:
    def __init__(self, jql: str, field_ids: dict[str, str], current_user: str) -> None:
        self.tokens = self._tokenize(jql)
        self.pos = 0
        self.field_ids = field_ids
        self.current_user = current_user

    @staticmethod
    def _tokenize(jql: str) -> list[tuple[str, str]]:
        tokens: list[tuple[str, str]] = []
        pos = 0
        while pos < len(jql):
            if jql[pos:].strip() == "":
                break
            m = _TOKEN.match(jql, pos)
            if not m:
                raise JqlError(f"Error in the JQL Query: unexpected text at position {pos}: {jql[pos:pos + 20]!r}")
            kind = m.lastgroup or ""
            text = m.group(kind)
            if kind == "str":
                text = re.sub(r"\\(.)", r"\1", text[1:-1])
            tokens.append((kind, text))
            pos = m.end()
        return tokens

    # --- доступ к токенам ---

    def _peek(self) -> tuple[str, str]:
        return self.tokens[self.pos] if self.pos < len(self.tokens) else ("end", "")

    def _next(self) -> tuple[str, str]:
        token = self._peek()
        self.pos += 1
        return token

    def _is_kw(self, word: str) -> bool:
        kind, text = self._peek()
        return kind == "word" and text.upper() == word

    def _take_kw(self, word: str) -> bool:
        if self._is_kw(word):
            self.pos += 1
            return True
        return False

    def _take_punct(self, char: str) -> bool:
        if self._peek() == ("punct", char):
            self.pos += 1
            return True
        return False

    def _expect_punct(self, char: str) -> None:
        if not self._take_punct(char):
            raise JqlError(f"Error in the JQL Query: expecting '{char}' but got '{self._peek()[1] or 'end of query'}'")

    # --- грамматика ---

    def parse(self) -> tuple[Predicate | None, list[tuple[str, bool]]]:
        condition = None
        if self._peek()[0] != "end" and not self._is_kw("ORDER"):
            condition = self._or()
        sorts: list[tuple[str, bool]] = []
        if self._take_kw("ORDER"):
            if not self._take_kw("BY"):
                raise JqlError("Error in the JQL Query: expecting 'BY' after 'ORDER'")
            while True:
                field = self._field()
                descending = self._take_kw("DESC")
                if not descending:
                    self._take_kw("ASC")
                sorts.append((field, descending))
                if not self._take_punct(","):
                    break
        if self._peek()[0] != "end":
            raise JqlError(f"Error in the JQL Query: unexpected '{self._peek()[1]}'")
        return condition, sorts

    def _or(self) -> Predicate:
        parts = [self._and()]
        while self._take_kw("OR"):
            parts.append(self._and())
        return parts[0] if len(parts) == 1 else lambda issue: any(p(issue) for p in parts)

    def _and(self) -> Predicate:
        parts = [self._not()]
        while self._take_kw("AND"):
            parts.append(self._not())
        return parts[0] if len(parts) == 1 else lambda issue: all(p(issue) for p in parts)

    def _not(self) -> Predicate:
        if self._take_kw("NOT"):
            inner = self._not()
            return lambda issue: not inner(issue)
        if self._take_punct("("):
            inner = self._or()
            self._expect_punct(")")
            return inner
        return self._clause()

    def _field(self) -> str:
        kind, text = self._next()
        if kind not in ("word", "str"):
            raise JqlError(f"Error in the JQL Query: expecting a field name but got '{text or 'end of query'}'")
        name = text.strip().casefold()
        m = re.fullmatch(r"cf\[(\d+)\]", name)
        if m:
            name = f"customfield_{m.group(1)}"
        name = _ALIASES.get(name, name)
        if name in _VIRTUAL_FIELDS:
            return name
        if name not in self.field_ids:
            raise JqlError(f"Field '{text}' does not exist or you do not have permission to view it.")
        return self.field_ids[name]

    def _operand(self) -> Any:
        """Значение справа от оператора; None означает EMPTY."""
        kind, text = self._next()
        if kind == "str":
            return text
        if kind != "word":
            raise JqlError(f"Error in the JQL Query: expecting a value but got '{text or 'end of query'}'")
        if self._take_punct("("):
            self._expect_punct(")")
            func = text.casefold()
            if func == "currentuser":
                return self.current_user
            if func == "now":
                return datetime.now().isoformat(timespec="minutes")
            raise JqlError(f"Unable to find JQL function '{text}()'.")
        if text.upper() in ("EMPTY", "NULL"):
            return None
        return text

    def _clause(self) -> Predicate:
        field = self._field()

        if self._take_kw("IS"):
            negated = self._take_kw("NOT")
            if self._operand() is not None:
                raise JqlError("Error in the JQL Query: IS and IS NOT work only with EMPTY or NULL")
            return lambda issue: bool(_values(issue, field)) == negated

        negated = self._take_kw("NOT")
        if self._take_kw("IN"):
            self._expect_punct("(")
            wanted = {_norm(v) for v in self._operand_list()}
            if negated:
                return lambda issue: self._none_equal(issue, field, wanted)
            return lambda issue: any(_norm(v) in wanted for v in _values(issue, field))
        if negated:
            raise JqlError("Error in the JQL Query: expecting 'IN' after 'NOT'")

        kind, op = self._next()
        if kind != "op":
            raise JqlError(f"Error in the JQL Query: expecting an operator but got '{op or 'end of query'}'")
        operand = self._operand()

        if operand is None:
            if op == "=":
                return lambda issue: not _values(issue, field)
            if op == "!=":
                return lambda issue: bool(_values(issue, field))
            raise JqlError(f"The operator '{op}' does not support EMPTY")
        if op == "=":
            return lambda issue: any(_norm(v) == _norm(operand) for v in _values(issue, field))
        if op == "!=":
            return lambda issue: self._none_equal(issue, field, {_norm(operand)})
        if op == "~":
            return lambda issue: any(_norm(operand) in _norm(v) for v in _values(issue, field))
        if op == "!~":
            return lambda issue: all(_norm(operand) not in _norm(v) for v in _values(issue, field))
        compare = _RELATIONAL[op]
        bound = _order_key(field, operand)
        return lambda issue: any(
            key[0] == bound[0] and compare(key, bound) for key in (_order_key(field, v) for v in _values(issue, field))
        )

    def _operand_list(self) -> list[Any]:
        items: list[Any] = []
        while True:
            value = self._operand()
            if value is not None:
                items.append(value)
            if self._take_punct(")"):
                return items
            self._expect_punct(",")

    @staticmethod
    def _none_equal(issue: Issue, field: str, wanted: set[str]) -> bool:
        # Как в Jira: «!=» и «NOT IN» не находят задачи с пустым полем.
        values = _values(issue, field)
        return bool(values) and all(_norm(v) not in wanted for v in values)


def run_jql(jql: str, issues: list[Issue], field_ids: list[str], current_user: str = "") -> list[Issue]:
    """Отфильтровать и отсортировать задачи (в формате ответа Jira) по JQL."""
    lookup = {name.casefold(): name for name in field_ids}
    condition, sorts = _Parser(jql or "", lookup, current_user).parse()
    result = [issue for issue in issues if condition is None or condition(issue)]
    for field, descending in reversed(sorts):

        def sort_key(issue: Issue, field: str = field) -> tuple:
            values = _values(issue, field)
            return (1, _order_key(field, values[0])) if values else (0,)

        result.sort(key=sort_key, reverse=descending)
    return result
