"""In-memory stand-in for the few budget_accounts / model_requests statements the ledger and gateway run.

It understands only the shapes those statements use: a SELECT by project/task/request_key,
INSERT INTO model_requests, and UPDATE ... SET with `col = :p`, `col = col +/- :p`,
`col = COALESCE(:p, col)`, `col = CAST(:p AS jsonb)`, literals and now(). It is not SQL.
"""

from __future__ import annotations

import copy
import json
import re
from contextlib import contextmanager
from typing import Any, Iterator


def prices_at(rate_micro: int, rate_tokens: int) -> dict[str, Any]:
    """Coding prices that bill measured tokens at a reservation's per-call rate, input and output alike.

    Coding usage is priced only from recorded prices (input and output micro-USD per million tokens
    with a pricing_version); a test row that pins a rate records the matching prices. Rounded down, so a
    call of t tokens still costs ceil(t * rate) for the call sizes these tests use (7/3: 2333333).
    """
    per_mtok = rate_micro * 1_000_000 // rate_tokens
    return {"pricing_version": f"test-{per_mtok}-{per_mtok}", "input_micro_usd_per_mtok": per_mtok, "output_micro_usd_per_mtok": per_mtok}


class _Result:
    def __init__(self, rows: list[dict[str, Any]], rowcount: int = 0) -> None:
        self._rows = rows
        self.rowcount = rowcount

    def mappings(self) -> "_Result":
        return self

    def one(self) -> dict[str, Any]:
        if len(self._rows) != 1:
            raise AssertionError(f"expected one row, got {len(self._rows)}")
        return self._rows[0]

    def first(self) -> dict[str, Any] | None:
        return self._rows[0] if self._rows else None

    def all(self) -> list[dict[str, Any]]:
        return list(self._rows)

    def scalar(self) -> Any:
        if not self._rows:
            return None
        return next(iter(self._rows[0].values()))


class FakeLedgerDatabase:
    def __init__(self) -> None:
        self.accounts: dict[tuple[str, str], dict[str, Any]] = {}
        self.requests: dict[tuple[str, str, str], dict[str, Any]] = {}

    def add_account(self, project_id: str, task_id: str, **values: Any) -> dict[str, Any]:
        row = {
            "id": len(self.accounts) + 1,
            "project_id": project_id,
            "task_id": task_id,
            "limit_micro_usd": 2_000_000,
            "spent_micro_usd": 0,
            "held_micro_usd": 0,
            "token_limit": 100_000,
            "tokens_used": 0,
            "tokens_held": 0,
            "max_requests": 48,
            "requests_admitted": 0,
            "verification_reserve_fraction": 0.2,
            "version": 0,
        }
        row.update(values)
        self.accounts[(project_id, task_id)] = row
        return row

    def add_request(self, project_id: str, task_id: str, request_key: str, **values: Any) -> dict[str, Any]:
        row = {
            "request_key": request_key,
            "request_sha256": "",
            "role": "coding",
            "model_alias": "coding",
            "reserved_micro_usd": 0,
            "actual_micro_usd": None,
            "input_tokens": None,
            "output_tokens": None,
            "usage_status": "reserved",
            "usage_detail": {},
            "model_revision": None,
            "pricing_version": None,
            "completed_at": None,
        }
        row.update(values)
        self.requests[(project_id, task_id, request_key)] = row
        return row

    def account(self, project_id: str, task_id: str) -> dict[str, Any]:
        return self.accounts[(project_id, task_id)]

    def request(self, project_id: str, task_id: str, request_key: str) -> dict[str, Any]:
        return self.requests[(project_id, task_id, request_key)]

    @contextmanager
    def transaction(self, **_scope: Any) -> Iterator["FakeLedgerDatabase"]:
        saved = copy.deepcopy((self.accounts, self.requests))
        try:
            yield self
        except BaseException:
            self.accounts, self.requests = saved
            raise

    def execute(self, clause: Any, params: dict[str, Any] | None = None) -> _Result:
        sql = " ".join(str(clause).split())
        params = dict(params or {})
        if sql.startswith("SELECT"):
            return self._select(sql, params)
        if sql.startswith("INSERT INTO model_requests"):
            return self._insert(sql, params)
        if sql.startswith("UPDATE"):
            return self._update(sql, params)
        raise AssertionError(f"unsupported statement: {sql}")

    def _select(self, sql: str, params: dict[str, Any]) -> _Result:
        if "FROM budget_accounts" in sql:
            row = self.accounts.get((params["project_id"], params["task_id"]))
            return _Result([] if row is None else [dict(row)])
        if sql.startswith("SELECT count(*) FROM model_requests") and "LIKE :prefix" in sql:
            prefix = params["prefix"].rstrip("%")
            count = sum(
                1
                for (project_id, task_id, key) in self.requests
                if project_id == params["project_id"] and task_id == params["task_id"] and key.startswith(prefix)
            )
            return _Result([{"count": count}])
        if sql.startswith("SELECT request_key FROM model_requests") and "LIKE :prefix" in sql:
            prefix = params["prefix"].rstrip("%")
            keys = sorted(
                key
                for (project_id, task_id, key), row in self.requests.items()
                if project_id == params["project_id"]
                and task_id == params["task_id"]
                and key.startswith(prefix)
                and ("role = 'coding'" not in sql or row.get("role") == "coding")
                and ("usage_status = 'reserved'" not in sql or row["usage_status"] == "reserved")
            )
            return _Result([{"request_key": key} for key in keys])
        if "FROM coding_jobs" in sql:
            # This fake keeps no coding_jobs table: the attempt under test still holds its fence.
            # CodingSqlFake models the table and answers this for real.
            return _Result([{"one": 1}])
        if "FROM model_requests" in sql:
            row = self.requests.get((params["project_id"], params["task_id"], params.get("request_key", "")))
            if row is None or ("usage_status = 'reserved'" in sql and row["usage_status"] != "reserved"):
                return _Result([])
            columns = sql[len("SELECT ") : sql.index(" FROM ")]
            if columns.strip() == "1":
                return _Result([{"one": 1}])
            picked = {}
            for name in (part.strip() for part in columns.split(",")):
                value = row[name]
                picked[name] = dict(value) if isinstance(value, dict) else value
            return _Result([picked])
        raise AssertionError(f"unsupported select: {sql}")

    def _insert(self, sql: str, params: dict[str, Any]) -> _Result:
        key = (params["project_id"], params["task_id"], params["request_key"])
        if key in self.requests:
            from sqlalchemy.exc import IntegrityError

            raise IntegrityError(sql, params, Exception("duplicate request_key"))
        self.add_request(
            params["project_id"],
            params["task_id"],
            params["request_key"],
            request_sha256=params.get("digest", ""),
            role=params.get("role", "coding_call"),
            model_alias=params.get("model_alias") or params.get("alias"),
            reserved_micro_usd=params.get("amount", 0),
            usage_status="reserved",
            usage_detail=json.loads(params["detail"]),
        )
        return _Result([], rowcount=1)

    def _update(self, sql: str, params: dict[str, Any]) -> _Result:
        table = sql.split()[1]
        if table == "capability_grants":
            # This fake keeps no grants: revoking a ticket's grants finds none to revoke.
            return _Result([], rowcount=0)
        assignments = sql[sql.index(" SET ") + 5 : sql.index(" WHERE ")]
        where = sql[sql.index(" WHERE ") + 7 :]
        if table == "budget_accounts":
            targets = [row for row in self.accounts.values() if row["id"] == params["id"]]
        elif table == "model_requests":
            row = self.requests.get((params["project_id"], params["task_id"], params["request_key"]))
            targets = [] if row is None else [row]
            if "usage_status = 'reserved'" in where:
                targets = [item for item in targets if item["usage_status"] == "reserved"]
        else:
            raise AssertionError(f"unsupported update: {sql}")
        for row in targets:
            for assignment in _split_assignments(assignments):
                _assign(row, assignment, params)
        return _Result([], rowcount=len(targets))


def _split_assignments(text: str) -> list[str]:
    parts: list[str] = []
    depth = 0
    current = ""
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        if char == "," and depth == 0:
            parts.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        parts.append(current.strip())
    return parts


def _assign(row: dict[str, Any], assignment: str, params: dict[str, Any]) -> None:
    column, expression = (part.strip() for part in assignment.split("=", 1))
    arithmetic = re.fullmatch(rf"{column} ([+-]) (:\w+|\d+)", expression)
    if arithmetic:
        operand = arithmetic.group(2)
        amount = params[operand[1:]] if operand.startswith(":") else int(operand)
        row[column] = row[column] + amount if arithmetic.group(1) == "+" else row[column] - amount
        return
    coalesce = re.fullmatch(rf"COALESCE\(:(\w+), {column}\)", expression)
    if coalesce:
        if params[coalesce.group(1)] is not None:
            row[column] = params[coalesce.group(1)]
        return
    cast = re.fullmatch(r"CAST\(:(\w+) AS jsonb\)", expression)
    if cast:
        row[column] = json.loads(params[cast.group(1)])
        return
    if expression.startswith(":"):
        row[column] = params[expression[1:]]
    elif expression == "NULL":
        row[column] = None
    elif expression == "now()":
        row[column] = "now"
    elif expression.startswith("'") and expression.endswith("'"):
        row[column] = expression[1:-1]
    elif re.fullmatch(r"\d+", expression):
        row[column] = int(expression)
    else:
        raise AssertionError(f"unsupported assignment: {assignment}")
