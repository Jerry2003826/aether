"""In-memory stand-in for the coding job, operation and ledger statements a takeover runs.

It extends FakeLedgerDatabase (budget_accounts / model_requests) with coding_jobs and operations
rows and a clock. The WHERE evaluator understands conjunctions of `[alias.]col = :p`,
`= [alias.]col`, `= 'lit'`, `= ANY(:p)`, `IN ('a', 'b')`, `IS NULL`, comparisons with now(), and
parenthesised ORs; SET understands `:p`, NULL, now(), `now() + make_interval(secs => :p)`, `col + n`
and CAST(:p AS jsonb). Statements on any other table are accepted and do nothing. It is not SQL.
"""

from __future__ import annotations

import copy
import json
import re
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from ledger_sql_fake import FakeLedgerDatabase, _Result, _split_assignments


class _Rows(_Result):
    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar_one(self) -> Any:
        # Only the task_events sequence asks for this on an otherwise unmodelled table.
        return 1 if not self._rows else next(iter(self._rows[0].values()))

    def scalars(self) -> _Rows:
        return _Rows([{"value": next(iter(row.values()))} for row in self._rows], self.rowcount)


class _Store:
    """ArtifactStore.put stand-in for the terminal publish."""

    def __init__(self) -> None:
        self.puts: list[str] = []

    def put(self, **kwargs: Any) -> dict[str, str]:
        self.puts.append(str(kwargs.get("classification")))
        return {"artifact_id": f"artifact-{len(self.puts)}"}


class CodingSqlFake(FakeLedgerDatabase):
    def __init__(self) -> None:
        super().__init__()
        self.now = 1_000.0
        self.jobs: dict[str, dict[str, Any]] = {}
        self.operations: dict[str, dict[str, Any]] = {}
        self.statements: list[str] = []

    def add_job(
        self,
        job_id: str,
        *,
        project_id: str,
        task_id: str,
        operation_id: str,
        fencing: int,
        state: str = "QUEUED",
        deadline: float | None = None,
        lease_until: float | None = None,
        operation_state: str = "PREPARED",
        objective: str = "Make value() return 42.",
    ) -> None:
        self.jobs[job_id] = {
            "id": job_id,
            "project_id": project_id,
            "task_id": task_id,
            "operation_id": operation_id,
            "base_artifact_id": "artifact-base",
            "fencing_token": fencing,
            "state": state,
            "deadline": self.now + 3600 if deadline is None else deadline,
            "last_heartbeat_at": None,
            "container_reference": None,
            "patch_artifact_id": None,
            "result_artifact_id": None,
            "events_artifact_id": None,
            "created_at": 0,
        }
        self.operations[operation_id] = {
            "id": operation_id,
            "project_id": project_id,
            "task_id": task_id,
            "state": operation_state,
            "lease_until": lease_until,
            "fencing_token": fencing,
            "objective": objective,
            "error_code": None,
            "result": None,
            "resolved_at": None,
        }

    @contextmanager
    def transaction(self, **_scope: Any) -> Iterator[CodingSqlFake]:
        saved = copy.deepcopy((self.accounts, self.requests, self.jobs, self.operations))
        try:
            yield self
        except BaseException:
            self.accounts, self.requests, self.jobs, self.operations = saved
            raise

    @contextmanager
    def begin_nested(self) -> Iterator[CodingSqlFake]:
        yield self

    def execute(self, clause: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(clause).split())
        params = dict(params or {})
        self.statements.append(sql)
        if re.match(r"UPDATE (coding_jobs|operations)\b", sql):
            return self._update_rows(sql, params)
        if sql.startswith("SELECT") and re.search(r" FROM coding_jobs\b", sql):
            return self._select_jobs(sql, params)
        if re.search(r"\b(budget_accounts|model_requests)\b", sql.split(" WHERE ")[0]):
            result = super().execute(clause, params)
            return _Rows(result._rows, result.rowcount)
        if sql.startswith("SELECT"):
            return _Rows([])
        return _Rows([], rowcount=1)

    # coding_jobs / operations
    def _select_jobs(self, sql: str, params: dict[str, Any]) -> _Result:
        match = re.fullmatch(
            r"SELECT (?P<columns>.+?) FROM coding_jobs(?: AS)?(?: (?P<alias>(?!WHERE|JOIN)\w+))?"
            r"(?: JOIN operations(?: AS)? (?P<oalias>\w+) ON (?P<on>.+?))? WHERE (?P<where>.+?)"
            r"(?: ORDER BY .+?)?(?: FOR (?:UPDATE|SHARE)(?: OF \w+)?)?",
            sql,
        )
        if match is None:
            raise AssertionError(f"unsupported coding_jobs select: {sql}")
        alias = match.group("alias") or "coding_jobs"
        oalias = match.group("oalias")
        where = match.group("where")
        if match.group("on"):
            where = f"{match.group('on')} AND {where}"
        rows = []
        for job in list(self.jobs.values()):
            envs = [{alias: job, None: job}]
            if oalias:
                envs = [{alias: job, oalias: op, None: job} for op in self.operations.values()]
            for env in envs:
                if self._holds(where, env, params):
                    rows.append(self._columns(match.group("columns"), job))
        return _Rows(rows)

    def _columns(self, columns: str, job: dict[str, Any]) -> dict[str, Any]:
        picked: dict[str, Any] = {}
        for item in _split_top(columns, ","):
            item = item.strip()
            if item == "1":
                picked["one"] = 1
                continue
            named = re.fullmatch(r"(.+) AS (\w+)", item)
            expression, name = (named.group(1), named.group(2)) if named else (item, None)
            if expression.startswith("(SELECT"):
                picked[name or "value"] = self.operations[job["operation_id"]]["objective"]
                continue
            column = expression.split("::")[0].split(".")[-1]
            picked[name or column] = job[column]
        return picked

    def _update_rows(self, sql: str, params: dict[str, Any]) -> _Result:
        match = re.fullmatch(
            r"UPDATE (?P<table>coding_jobs|operations)(?: AS (?P<alias>\w+))? SET (?P<set>.+?)"
            r"(?: FROM coding_jobs AS (?P<jalias>\w+))? WHERE (?P<where>.+)",
            sql,
        )
        if match is None:
            raise AssertionError(f"unsupported update: {sql}")
        table = match.group("table")
        alias = match.group("alias") or table
        rows = self.jobs if table == "coding_jobs" else self.operations
        hit: list[dict[str, Any]] = []
        for row in rows.values():
            envs = [{alias: row, None: row}]
            if match.group("jalias"):
                envs = [{alias: row, match.group("jalias"): job, None: row} for job in self.jobs.values()]
            if any(self._holds(match.group("where"), env, params) for env in envs):
                hit.append(row)
        for row in hit:
            for assignment in _split_assignments(match.group("set")):
                self._assign(row, assignment, params)
        return _Rows([], rowcount=len(hit))

    def _assign(self, row: dict[str, Any], assignment: str, params: dict[str, Any]) -> None:
        column, expression = (part.strip() for part in assignment.split("=", 1))
        if expression.startswith(":"):
            row[column] = params[expression[1:]]
        elif expression == "NULL":
            row[column] = None
        elif expression == "now()":
            row[column] = self.now
        elif (interval := re.fullmatch(r"now\(\) \+ make_interval\(secs => :(\w+)\)", expression)) is not None:
            row[column] = self.now + float(params[interval.group(1)])
        elif (step := re.fullmatch(rf"{column} \+ (\d+|:\w+)", expression)) is not None:
            operand = step.group(1)
            row[column] = row[column] + (int(params[operand[1:]]) if operand.startswith(":") else int(operand))
        elif (cast := re.fullmatch(r"CAST\(:(\w+) AS jsonb\)", expression)) is not None:
            row[column] = json.loads(params[cast.group(1)])
        elif expression.startswith("'") and expression.endswith("'"):
            row[column] = expression[1:-1]
        else:
            raise AssertionError(f"unsupported assignment: {assignment}")

    def _holds(self, condition: str, env: dict[str | None, dict[str, Any]], params: dict[str, Any]) -> bool:
        condition = condition.strip()
        parts = _split_top(condition, " AND ")
        if len(parts) > 1:
            return all(self._holds(part, env, params) for part in parts)
        if condition.startswith("(") and _closes_at_end(condition):
            inner = condition[1:-1]
            options = _split_top(inner, " OR ")
            return any(self._holds(option, env, params) for option in options)
        if (null := re.fullmatch(r"(\S+) IS NULL", condition)) is not None:
            return self._value(null.group(1), env, params) is None
        if (many := re.fullmatch(r"(\S+) = ANY\(:(\w+)\)", condition)) is not None:
            return self._value(many.group(1), env, params) in list(params[many.group(2)])
        if (among := re.fullmatch(r"(\S+) IN \((.+)\)", condition)) is not None:
            options = [item.strip().strip("'") for item in among.group(2).split(",")]
            return self._value(among.group(1), env, params) in options
        compare = re.fullmatch(r"(\S+) (=|<=|<|>=|>) (.+)", condition)
        if compare is None:
            raise AssertionError(f"unsupported condition: {condition}")
        left = self._value(compare.group(1), env, params)
        right = self._value(compare.group(3), env, params)
        if left is None or right is None:
            return False
        op = compare.group(2)
        if op == "=":
            return str(left) == str(right) if isinstance(left, str) or isinstance(right, str) else left == right
        return {"<=": left <= right, "<": left < right, ">=": left >= right, ">": left > right}[op]

    def _value(self, token: str, env: dict[str | None, dict[str, Any]], params: dict[str, Any]) -> Any:
        token = token.strip()
        if token.startswith(":"):
            return params[token[1:]]
        if token == "now()":
            return self.now
        if token.startswith("'") and token.endswith("'"):
            return token[1:-1]
        if re.fullmatch(r"-?\d+", token):
            return int(token)
        alias, _, column = token.rpartition(".")
        row = env[alias or None]
        if column not in row:
            raise AssertionError(f"unknown column {token}")
        return row[column]


def _split_top(text: str, separator: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and text.startswith(separator, index):
            parts.append(text[start:index])
            index += len(separator)
            start = index
            continue
        index += 1
    parts.append(text[start:])
    return [part for part in parts if part.strip()]


def _closes_at_end(text: str) -> bool:
    depth = 0
    for index, char in enumerate(text):
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth == 0 and index != len(text) - 1:
                return False
    return True


Store = _Store
