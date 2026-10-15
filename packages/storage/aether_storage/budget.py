"""Transactional budget reservations. Unknown usage stays held and is never stored as zero."""

from __future__ import annotations

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_storage.db import Database


class BudgetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BudgetLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    def open_account(self, *, owner_id: str, project_id: str, task_id: str, limit_micro_usd: int, token_limit: int, max_requests: int, verification_reserve_fraction: float = 0.2) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO budget_accounts (project_id, task_id, limit_micro_usd, token_limit, max_requests, verification_reserve_fraction) "
                    "VALUES (:project_id, :task_id, :limit_micro_usd, :token_limit, :max_requests, :fraction)"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "limit_micro_usd": limit_micro_usd,
                    "token_limit": token_limit,
                    "max_requests": max_requests,
                    "fraction": verification_reserve_fraction,
                },
            )

    def reserve(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, amount: int, role: str, tokens: int = 1, verification: bool = False) -> bool:
        if amount < 0 or tokens < 0:
            raise BudgetError("invalid_amount")
        digest = "a" * 64
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text(
                    "SELECT id, limit_micro_usd, spent_micro_usd, held_micro_usd, token_limit, tokens_used, "
                    "max_requests, requests_admitted, verification_reserve_fraction "
                    "FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            existing = conn.execute(
                text("SELECT reserved_micro_usd, usage_status FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().first()
            if existing is not None:
                if existing["reserved_micro_usd"] != amount:
                    raise BudgetError("idempotency_mismatch")
                return True
            fraction = float(account["verification_reserve_fraction"])
            ceiling = account["limit_micro_usd"] if verification else int(account["limit_micro_usd"] * (1 - fraction))
            token_ceiling = account["token_limit"] if verification else int(account["token_limit"] * (1 - fraction))
            if account["spent_micro_usd"] + account["held_micro_usd"] + amount > ceiling:
                return False
            if account["tokens_used"] + tokens > token_ceiling or account["requests_admitted"] + 1 > account["max_requests"]:
                return False
            try:
                conn.execute(
                    text(
                        "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status) "
                        "VALUES (:project_id, :task_id, :request_key, :digest, :role, :role, :amount, 'reserved')"
                    ),
                    {"project_id": project_id, "task_id": task_id, "request_key": request_key, "digest": digest, "role": role, "amount": amount},
                )
            except IntegrityError as exc:
                raise BudgetError("idempotency_mismatch") from exc
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = held_micro_usd + :amount, requests_admitted = requests_admitted + 1, version = version + 1 "
                    "WHERE id = :id"
                ),
                {"amount": amount, "id": account["id"]},
            )
        return True

    def settle(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, actual: int, input_tokens: int, output_tokens: int) -> None:
        if actual < 0:
            raise BudgetError("invalid_amount")
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT id FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            request = conn.execute(
                text("SELECT reserved_micro_usd, usage_status, actual_micro_usd FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().one()
            if request["usage_status"] == "known":
                if request["actual_micro_usd"] != actual:
                    raise BudgetError("conflicting_settlement")
                return
            if request["usage_status"] == "unknown":
                raise BudgetError("unknown_not_zero")
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = 'known', actual_micro_usd = :actual, input_tokens = :input_tokens, output_tokens = :output_tokens, completed_at = now() "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {"actual": actual, "input_tokens": input_tokens, "output_tokens": output_tokens, "project_id": project_id, "task_id": task_id, "request_key": request_key},
            )
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = held_micro_usd - :reserved, spent_micro_usd = spent_micro_usd + :actual, "
                    "tokens_used = tokens_used + :tokens, version = version + 1 WHERE id = :id"
                ),
                {"reserved": request["reserved_micro_usd"], "actual": actual, "tokens": input_tokens + output_tokens, "id": account["id"]},
            )

    def mark_unknown(self, *, owner_id: str, project_id: str, task_id: str, request_key: str) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text("SELECT id FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            )
            updated = conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = 'unknown', actual_micro_usd = NULL "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).rowcount
            if updated != 1:
                raise BudgetError("not_reserved")

    def snapshot(self, *, owner_id: str, project_id: str, task_id: str) -> dict:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT limit_micro_usd, spent_micro_usd, held_micro_usd, tokens_used, requests_admitted FROM budget_accounts WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).mappings().one()
            unknown = conn.execute(
                text("SELECT count(*) FROM model_requests WHERE task_id = :task_id AND usage_status = 'unknown'"),
                {"task_id": task_id},
            ).scalar_one()
        return {
            "limit_micro_usd": account["limit_micro_usd"],
            "actual_known_micro_usd": account["spent_micro_usd"],
            "held_micro_usd": account["held_micro_usd"],
            "unknown_request_count": unknown,
            "tokens_used": account["tokens_used"],
            "request_count": account["requests_admitted"],
        }
