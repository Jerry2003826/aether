"""Transactional budget reservations. Unknown usage stays held and is never stored as zero."""

from __future__ import annotations

import json

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_domain.security import sha256_text
from aether_storage.db import Database


def _detail(value: object) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


class BudgetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class BudgetLedger:
    def __init__(self, database: Database) -> None:
        self.database = database

    def open_account(self, *, owner_id: str, project_id: str, task_id: str, limit_micro_usd: int, token_limit: int, max_requests: int, verification_reserve_fraction: float = 0.2, active_seconds: int = 1800) -> None:
        if active_seconds < 1:
            raise BudgetError("invalid_amount")
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            conn.execute(
                text(
                    "INSERT INTO budget_accounts (project_id, task_id, limit_micro_usd, token_limit, max_requests, verification_reserve_fraction, active_seconds) "
                    "VALUES (:project_id, :task_id, :limit_micro_usd, :token_limit, :max_requests, :fraction, :active_seconds)"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "limit_micro_usd": limit_micro_usd,
                    "token_limit": token_limit,
                    "max_requests": max_requests,
                    "fraction": verification_reserve_fraction,
                    "active_seconds": active_seconds,
                },
            )

    def reserve(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, amount: int, role: str, tokens: int = 1, verification: bool = False, request: dict | None = None, model_alias: str | None = None) -> bool:
        if amount < 0 or tokens < 0:
            raise BudgetError("invalid_amount")
        digest = sha256_text(json.dumps({
            "project_id": project_id,
            "task_id": task_id,
            "request_key": request_key,
            "amount": amount,
            "role": role,
            "tokens": tokens,
            "verification": verification,
            "model_alias": model_alias or role,
            "request": request or {},
        }, sort_keys=True, separators=(",", ":")))
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text(
                    "SELECT id, limit_micro_usd, spent_micro_usd, held_micro_usd, token_limit, tokens_used, tokens_held, "
                    "max_requests, requests_admitted, verification_reserve_fraction "
                    "FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            existing = conn.execute(
                text("SELECT reserved_micro_usd, request_sha256, usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().first()
            if existing is not None:
                if existing["reserved_micro_usd"] != amount or existing["request_sha256"] != digest:
                    raise BudgetError("idempotency_mismatch")
                return True
            fraction = float(account["verification_reserve_fraction"])
            ceiling = account["limit_micro_usd"] if verification else int(account["limit_micro_usd"] * (1 - fraction))
            token_ceiling = account["token_limit"] if verification else int(account["token_limit"] * (1 - fraction))
            if account["spent_micro_usd"] + account["held_micro_usd"] + amount > ceiling:
                return False
            if account["tokens_used"] + account["tokens_held"] + tokens > token_ceiling or account["requests_admitted"] + 1 > account["max_requests"]:
                return False
            try:
                conn.execute(
                    text(
                        "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status, usage_detail) "
                        "VALUES (:project_id, :task_id, :request_key, :digest, :role, :model_alias, :amount, 'reserved', CAST(:detail AS jsonb))"
                    ),
                    {
                        "project_id": project_id,
                        "task_id": task_id,
                        "request_key": request_key,
                        "digest": digest,
                        "role": role,
                        "model_alias": model_alias or role,
                        "amount": amount,
                        "detail": json.dumps({"reserved_tokens": tokens}),
                    },
                )
            except IntegrityError as exc:
                raise BudgetError("idempotency_mismatch") from exc
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = held_micro_usd + :amount, tokens_held = tokens_held + :tokens, "
                    "requests_admitted = requests_admitted + 1, version = version + 1 WHERE id = :id"
                ),
                {"amount": amount, "tokens": tokens, "id": account["id"]},
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
                text("SELECT reserved_micro_usd, usage_status, actual_micro_usd, usage_detail FROM model_requests WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().one()
            if request["usage_status"] == "known":
                if request["actual_micro_usd"] != actual:
                    raise BudgetError("conflicting_settlement")
                return
            if request["usage_status"] == "unknown":
                raise BudgetError("unknown_not_zero")
            if actual > request["reserved_micro_usd"]:
                raise BudgetError("actual_exceeds_reservation")
            detail = request["usage_detail"] or {}
            if isinstance(detail, str):
                detail = json.loads(detail)
            reserved_tokens = int(detail.get("reserved_tokens") or 0)
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
                    "tokens_held = tokens_held - :reserved_tokens, tokens_used = tokens_used + :tokens, version = version + 1 WHERE id = :id"
                ),
                {
                    "reserved": request["reserved_micro_usd"],
                    "actual": actual,
                    "reserved_tokens": reserved_tokens,
                    "tokens": input_tokens + output_tokens,
                    "id": account["id"],
                },
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

    def release_unsent(self, *, owner_id: str, project_id: str, task_id: str, request_key: str) -> None:
        """Drop a reservation that never left this process."""
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT id FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            request = conn.execute(
                text(
                    "SELECT reserved_micro_usd, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().first()
            if request is None:
                return
            detail = _detail(request["usage_detail"])
            reserved_tokens = int(detail.get("reserved_tokens") or 0)
            detail["dispatch_state"] = "cancelled_before_dispatch"
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = 'cancelled_before_dispatch', actual_micro_usd = NULL, usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {"detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
            )
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = held_micro_usd - :amount, tokens_held = tokens_held - :tokens, version = version + 1 WHERE id = :id"
                ),
                {"amount": int(request["reserved_micro_usd"]), "tokens": reserved_tokens, "id": account["id"]},
            )

    def _runnable(self, conn, project_id: str, task_id: str) -> bool:
        row = conn.execute(
            text("SELECT status, resume_state FROM tasks WHERE project_id = :project_id AND id = :id FOR UPDATE"),
            {"project_id": project_id, "id": task_id},
        ).mappings().first()
        if row is None:
            return False
        if str(row["status"]) not in {"RUNNING", "VERIFYING"}:
            return False
        return str(row["resume_state"] or "") not in {"pause_requested", "cancel_requested"}

    def acquire_dispatch_permit(self, *, owner_id: str, project_id: str, task_id: str, request_key: str) -> bool:
        """Mark a reserved call permitted only while the task is still runnable.

        A pause or cancel that commits after this returns can still revoke the
        permit before consume_dispatch_permit flips it to sending.
        """
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            if not self._runnable(conn, project_id, task_id):
                return False
            request = conn.execute(
                text(
                    "SELECT usage_status, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().first()
            if request is None or request["usage_status"] != "reserved":
                return False
            detail = _detail(request["usage_detail"])
            if detail.get("dispatch_state") not in {"dispatched", "permitted"}:
                return False
            detail["dispatch_state"] = "permitted"
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {"detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
            )
        return True

    def consume_dispatch_permit(self, *, owner_id: str, project_id: str, task_id: str, request_key: str) -> bool:
        """Flip a still-permitted call to sending. A committed stop makes this return false."""
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            if not self._runnable(conn, project_id, task_id):
                return False
            request = conn.execute(
                text(
                    "SELECT usage_status, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().first()
            if request is None or request["usage_status"] != "reserved":
                return False
            detail = _detail(request["usage_detail"])
            if detail.get("dispatch_state") != "permitted":
                return False
            detail["dispatch_state"] = "sending"
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                ),
                {"detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
            )
        return True

    def revoke_unsent(self, *, owner_id: str, project_id: str, task_id: str) -> None:
        """Release reservations that have not started a provider call."""
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT id FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().first()
            if account is None:
                return
            rows = conn.execute(
                text(
                    "SELECT request_key, reserved_micro_usd, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND usage_status = 'reserved' FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().all()
            for request in rows:
                detail = _detail(request["usage_detail"])
                if detail.get("dispatch_state") == "sending":
                    continue
                reserved_tokens = int(detail.get("reserved_tokens") or 0)
                detail["dispatch_state"] = "cancelled_before_dispatch"
                conn.execute(
                    text(
                        "UPDATE model_requests SET usage_status = 'cancelled_before_dispatch', actual_micro_usd = NULL, usage_detail = CAST(:detail AS jsonb) "
                        "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                    ),
                    {
                        "detail": json.dumps(detail),
                        "project_id": project_id,
                        "task_id": task_id,
                        "request_key": request["request_key"],
                    },
                )
                conn.execute(
                    text(
                        "UPDATE budget_accounts SET held_micro_usd = held_micro_usd - :amount, tokens_held = tokens_held - :tokens, version = version + 1 WHERE id = :id"
                    ),
                    {"amount": int(request["reserved_micro_usd"]), "tokens": reserved_tokens, "id": account["id"]},
                )

    def prepare_model_call(
        self,
        *,
        owner_id: str,
        project_id: str,
        task_id: str,
        request_key: str,
        amount: int,
        role: str,
        tokens: int,
        model_alias: str,
        prompt_sha256: str,
        output_cap: int,
        source_snapshot_sha256: str,
    ) -> dict[str, object]:
        """Reserve once, then either send, reuse a stored response, or stop.

        A previous reservation is not permission to call the provider again.
        """
        if amount < 0 or tokens < 0:
            raise BudgetError("invalid_amount")
        request = {
            "alias": model_alias,
            "prompt_sha256": prompt_sha256,
            "max_tokens": output_cap,
            "source_snapshot_sha256": source_snapshot_sha256,
        }
        digest = sha256_text(json.dumps({
            "project_id": project_id,
            "task_id": task_id,
            "request_key": request_key,
            "model_alias": model_alias,
            "request": request,
        }, sort_keys=True, separators=(",", ":")))
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text(
                    "SELECT id, limit_micro_usd, spent_micro_usd, held_micro_usd, token_limit, tokens_used, tokens_held, "
                    "max_requests, requests_admitted, verification_reserve_fraction "
                    "FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            existing = conn.execute(
                text(
                    "SELECT usage_status, request_sha256, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().first()
            if existing is not None:
                detail = _detail(existing["usage_detail"])
                if detail.get("prompt_sha256") not in {None, prompt_sha256}:
                    return {"action": "conflict"}
                if detail.get("dispatch_state") == "response_recorded":
                    return {"action": "reuse", "book_result": str(detail.get("book_result") or "replayed"), "response": detail}
                if existing["usage_status"] == "cancelled_before_dispatch":
                    fraction = float(account["verification_reserve_fraction"])
                    ceiling = int(account["limit_micro_usd"] * (1 - fraction))
                    token_ceiling = int(account["token_limit"] * (1 - fraction))
                    if output_cap < 1 or amount < 1 or tokens < 1:
                        return {"action": "denied"}
                    if account["spent_micro_usd"] + account["held_micro_usd"] + amount > ceiling:
                        return {"action": "denied"}
                    if account["tokens_used"] + account["tokens_held"] + tokens > token_ceiling:
                        return {"action": "denied"}
                    detail.update(
                        {
                            "reserved_tokens": tokens,
                            "dispatch_state": "dispatched",
                            "prompt_sha256": prompt_sha256,
                            "output_cap": output_cap,
                            "source_snapshot_sha256": source_snapshot_sha256,
                        }
                    )
                    conn.execute(
                        text(
                            "UPDATE model_requests SET usage_status = 'reserved', reserved_micro_usd = :amount, usage_detail = CAST(:detail AS jsonb) "
                            "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                        ),
                        {"amount": amount, "detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
                    )
                    conn.execute(
                        text(
                            "UPDATE budget_accounts SET held_micro_usd = held_micro_usd + :amount, tokens_held = tokens_held + :tokens, version = version + 1 WHERE id = :id"
                        ),
                        {"amount": amount, "tokens": tokens, "id": account["id"]},
                    )
                    return {"action": "send"}
                if existing["usage_status"] == "reserved":
                    conn.execute(
                        text(
                            "UPDATE model_requests SET usage_status = 'unknown', actual_micro_usd = NULL "
                            "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
                        ),
                        {"project_id": project_id, "task_id": task_id, "request_key": request_key},
                    )
                return {"action": "unreconciled"}
            if output_cap < 1 or amount < 1 or tokens < 1:
                return {"action": "denied"}
            fraction = float(account["verification_reserve_fraction"])
            ceiling = int(account["limit_micro_usd"] * (1 - fraction))
            token_ceiling = int(account["token_limit"] * (1 - fraction))
            if account["spent_micro_usd"] + account["held_micro_usd"] + amount > ceiling:
                return {"action": "denied"}
            if account["tokens_used"] + account["tokens_held"] + tokens > token_ceiling or account["requests_admitted"] + 1 > account["max_requests"]:
                return {"action": "denied"}
            detail = {
                "reserved_tokens": tokens,
                "dispatch_state": "dispatched",
                "prompt_sha256": prompt_sha256,
                "output_cap": output_cap,
                "source_snapshot_sha256": source_snapshot_sha256,
            }
            conn.execute(
                text(
                    "INSERT INTO model_requests (project_id, task_id, request_key, request_sha256, role, model_alias, reserved_micro_usd, usage_status, usage_detail) "
                    "VALUES (:project_id, :task_id, :request_key, :digest, :role, :model_alias, :amount, 'reserved', CAST(:detail AS jsonb))"
                ),
                {
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": request_key,
                    "digest": digest,
                    "role": role,
                    "model_alias": model_alias,
                    "amount": amount,
                    "detail": json.dumps(detail),
                },
            )
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = held_micro_usd + :amount, tokens_held = tokens_held + :tokens, "
                    "requests_admitted = requests_admitted + 1, version = version + 1 WHERE id = :id"
                ),
                {"amount": amount, "tokens": tokens, "id": account["id"]},
            )
        return {"action": "send"}

    def record_model_response(
        self,
        *,
        owner_id: str,
        project_id: str,
        task_id: str,
        request_key: str,
        output: str,
        input_tokens: int | None,
        output_tokens: int | None,
        cost_micro_usd: int | None,
        upstream_model: str | None,
        requested_alias: str | None,
    ) -> str:
        """Store the provider response before any later artifact write.

        Known tokens are booked even when the price is missing. A reported
        price above the reservation is kept and not settled as if it fit.
        """
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT id FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            request = conn.execute(
                text(
                    "SELECT reserved_micro_usd, usage_status, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().one()
            detail = _detail(request["usage_detail"])
            if detail.get("dispatch_state") == "response_recorded":
                return str(detail.get("book_result") or "replayed")
            reserved_tokens = int(detail.get("reserved_tokens") or 0)
            reserved_cost = int(request["reserved_micro_usd"])
            reported_tokens = None if input_tokens is None or output_tokens is None else input_tokens + output_tokens
            tokens_fit = reported_tokens is not None and reported_tokens <= reserved_tokens
            if tokens_fit and cost_micro_usd is not None and cost_micro_usd <= reserved_cost:
                book_result = "settled"
                usage_status = "known"
                actual = cost_micro_usd
            elif tokens_fit and cost_micro_usd is None:
                book_result = "held"
                usage_status = "unknown"
                actual = None
            else:
                book_result = "overrun"
                usage_status = "observed"
                actual = cost_micro_usd
            detail.update(
                {
                    "dispatch_state": "response_recorded",
                    "output": output,
                    "book_result": book_result,
                    "reported_input_tokens": input_tokens,
                    "reported_output_tokens": output_tokens,
                    "reported_cost_micro_usd": cost_micro_usd,
                    "cost_source": "provider_response" if cost_micro_usd is not None else "unreported",
                    "reconciliation": "unreconciled" if book_result == "overrun" else "booked",
                    "upstream_model": upstream_model,
                    "requested_alias": requested_alias,
                }
            )
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = :usage_status, actual_micro_usd = :actual, "
                    "input_tokens = :input_tokens, output_tokens = :output_tokens, model_revision = :upstream, "
                    "usage_detail = CAST(:detail AS jsonb), completed_at = CASE WHEN :usage_status = 'known' THEN now() ELSE completed_at END "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {
                    "usage_status": usage_status,
                    "actual": actual,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "upstream": upstream_model,
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": request_key,
                },
            )
            if book_result == "settled":
                conn.execute(
                    text(
                        "UPDATE budget_accounts SET held_micro_usd = held_micro_usd - :reserved, spent_micro_usd = spent_micro_usd + :actual, "
                        "tokens_held = tokens_held - :reserved_tokens, tokens_used = tokens_used + :tokens, version = version + 1 WHERE id = :id"
                    ),
                    {
                        "reserved": reserved_cost,
                        "actual": cost_micro_usd,
                        "reserved_tokens": reserved_tokens,
                        "tokens": reported_tokens,
                        "id": account["id"],
                    },
                )
            elif tokens_fit:
                conn.execute(
                    text(
                        "UPDATE budget_accounts SET tokens_held = tokens_held - :reserved_tokens, tokens_used = tokens_used + :tokens, "
                        "version = version + 1 WHERE id = :id"
                    ),
                    {"reserved_tokens": reserved_tokens, "tokens": reported_tokens, "id": account["id"]},
                )
            elif reported_tokens is not None:
                conn.execute(
                    text(
                        "UPDATE budget_accounts SET tokens_held = tokens_held - :reserved_tokens, tokens_used = tokens_used + :tokens, "
                        "version = version + 1 WHERE id = :id"
                    ),
                    {"reserved_tokens": reserved_tokens, "tokens": reported_tokens, "id": account["id"]},
                )
        return book_result

    def snapshot(self, *, owner_id: str, project_id: str, task_id: str) -> dict:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT limit_micro_usd, spent_micro_usd, held_micro_usd, tokens_used, tokens_held, requests_admitted FROM budget_accounts WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).mappings().one()
            usage = conn.execute(
                text(
                    "SELECT COUNT(*) FILTER (WHERE usage_status = 'unknown') AS unknown_request_count, "
                    "COUNT(*) FILTER (WHERE usage_status = 'observed') AS observed_request_count, "
                    "COALESCE(SUM(actual_micro_usd) FILTER (WHERE usage_status = 'observed'), 0) AS observed_micro_usd "
                    "FROM model_requests WHERE task_id = :task_id"
                ),
                {"task_id": task_id},
            ).mappings().one()
        return {
            "limit_micro_usd": account["limit_micro_usd"],
            "actual_known_micro_usd": account["spent_micro_usd"],
            "held_micro_usd": account["held_micro_usd"],
            "unknown_request_count": int(usage["unknown_request_count"]),
            "observed_request_count": int(usage["observed_request_count"]),
            "observed_micro_usd": int(usage["observed_micro_usd"]),
            "tokens_used": account["tokens_used"],
            "tokens_held": account["tokens_held"],
            "request_count": account["requests_admitted"],
        }
