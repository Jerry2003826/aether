"""Transactional budget reservations. Unknown usage stays held and is never stored as zero."""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from aether_domain.security import sha256_text
from aether_storage.db import Database


UNSENT_DISPATCH_STATES = frozenset({"dispatched", "permitted"})
# Run first inside a settling transaction, before any ledger row is locked: False means the caller
# no longer holds what it is settling for (a coding attempt whose fencing token moved on).
SettleGuard = Callable[[Any], bool]


def _require_guard(conn, guard: SettleGuard | None) -> None:
    if guard is not None and not guard(conn):
        raise BudgetError("fence_lost")


def _detail(value: object) -> dict:
    if isinstance(value, str):
        return json.loads(value)
    if isinstance(value, dict):
        return dict(value)
    return {}


def _reservation_detail(tokens: int, extra: dict | None) -> dict:
    detail = {"reserved_tokens": tokens}
    if not extra:
        return detail
    for key, value in extra.items():
        if key != "reserved_tokens":
            detail[key] = value
    return detail


def _cancel_before_dispatch(conn, *, account_id: object, project_id: str, task_id: str, request_key: str, amount: int, detail: dict) -> None:
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
        {"amount": amount, "tokens": reserved_tokens, "id": account_id},
    )


def _count(value: object) -> int:
    return value if type(value) is int and value > 0 else 0


def coding_call_micro_usd(tokens: int, detail: dict) -> int:
    """Micro-USD for one coding call of `tokens` tokens, rounded up.

    The rate is the one recorded on the reservation when it was made:
    call_reserve_micro_usd / call_reserve_tokens. A row without both (written before the rate
    was recorded) is priced at 1 micro-USD per token, as it always was.
    """
    if tokens <= 0:
        return 0
    per_tokens = _count(detail.get("call_reserve_tokens"))
    per_micro = _count(detail.get("call_reserve_micro_usd"))
    if not per_tokens or not per_micro:
        return tokens
    return (tokens * per_micro + per_tokens - 1) // per_tokens


# Per-call money is kept next to each token counter on a coding reservation: <kind>_micro_usd is
# the sum of the calls' rounded-up amounts, <kind>_priced_tokens the tokens those amounts cover.
COST_KINDS = ("measured", "unknown_reserved", "pending")


def add_call_cost(detail: dict, kind: str, tokens: int) -> None:
    """Price one call of `tokens` on its own and add it to `kind`."""
    if tokens <= 0:
        return
    detail[f"{kind}_micro_usd"] = _count(detail.get(f"{kind}_micro_usd")) + coding_call_micro_usd(tokens, detail)
    detail[f"{kind}_priced_tokens"] = _count(detail.get(f"{kind}_priced_tokens")) + tokens


def remove_call_cost(detail: dict, kind: str, tokens: int) -> None:
    """Take one call of `tokens` back out of `kind` (a pending call that finished either way)."""
    if tokens <= 0:
        return
    priced = _count(detail.get(f"{kind}_priced_tokens"))
    if not priced:
        return
    detail[f"{kind}_priced_tokens"] = max(0, priced - tokens)
    detail[f"{kind}_micro_usd"] = max(0, _count(detail.get(f"{kind}_micro_usd")) - coding_call_micro_usd(tokens, detail))


def move_call_cost(detail: dict, source: str, target: str) -> None:
    """Move every call priced under `source` to `target`, keeping each call's own rounding."""
    detail[f"{target}_micro_usd"] = _count(detail.get(f"{target}_micro_usd")) + _count(detail.get(f"{source}_micro_usd"))
    detail[f"{target}_priced_tokens"] = _count(detail.get(f"{target}_priced_tokens")) + _count(detail.get(f"{source}_priced_tokens"))
    detail[f"{source}_micro_usd"] = 0
    detail[f"{source}_priced_tokens"] = 0


def priced_micro_usd(detail: dict, kind: str, tokens: int) -> int:
    """Micro-USD for `tokens` of `kind`: the per-call amounts recorded for them, and for any tokens
    no call priced (a row written before calls were priced one by one) one rounded-up amount."""
    if tokens <= 0:
        return 0
    covered = min(_count(detail.get(f"{kind}_priced_tokens")), tokens)
    priced = _count(detail.get(f"{kind}_micro_usd")) if covered else 0
    return priced + coding_call_micro_usd(tokens - covered, detail)


def _unknown_charge(
    unknown_tokens: int, unknown_micro: int, *, measured_tokens: int, measured_micro: int, reserved_tokens: int, reserved_cost: int
) -> tuple[int, int]:
    """Tokens and micro-USD to book for calls whose usage is unknown.

    Each unknown call is charged what was reserved for it, priced at the configured rate one call at
    a time (the caller sums those amounts into unknown_micro). Each unit is capped on its own by what
    is still held after the measured usage: tokens by tokens, money by money.
    """
    if unknown_tokens <= 0:
        return 0, 0
    tokens = min(unknown_tokens, max(0, reserved_tokens - measured_tokens))
    micro = min(unknown_micro, max(0, reserved_cost - measured_micro))
    return tokens, micro


def _replay_matches(existing, amount: int) -> bool:
    """True when amount is what this row was first reserved with, before any top-up.

    A replay (another worker re-running the same request_key) compares against that,
    so a top-up does not turn the replay into idempotency_mismatch and strand the
    enlarged hold. A row topped up before extended_micro_usd was recorded has no
    exact figure; the request digest, which covers the original amount, decides it
    as long as the row still holds at least that much.
    """
    reserved = int(existing["reserved_micro_usd"])
    detail = _detail(existing["usage_detail"])
    if not int(detail.get("reservation_extensions") or 0):
        return reserved == amount
    if "extended_micro_usd" in detail:
        return reserved - int(detail.get("extended_micro_usd") or 0) == amount
    return reserved >= amount


class BudgetError(Exception):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


def reservation_can_grow(account: dict, *, amount: int, tokens: int) -> bool:
    """True when this task can hold another non-verification reservation.

    The ceiling matches reserve(): verification keeps its fraction, and a coding
    top-up uses the remainder. Growing does not admit another request.
    """
    if amount < 0 or tokens < 0:
        raise BudgetError("invalid_amount")
    fraction = float(account["verification_reserve_fraction"])
    money_ceiling = int(account["limit_micro_usd"] * (1 - fraction))
    token_ceiling = int(account["token_limit"] * (1 - fraction))
    spent = int(account["spent_micro_usd"]) + int(account["held_micro_usd"]) + amount
    held_tokens = int(account["tokens_used"]) + int(account["tokens_held"]) + tokens
    return spent <= money_ceiling and held_tokens <= token_ceiling


def apply_reservation_extension(
    conn,
    *,
    account_id: object,
    project_id: str,
    task_id: str,
    request_key: str,
    amount: int,
    tokens: int,
    detail: dict,
) -> None:
    """Add to a reserved row already locked by the caller. The account row is locked too."""
    if amount < 0 or tokens < 1:
        raise BudgetError("invalid_amount")
    detail["reserved_tokens"] = int(detail.get("reserved_tokens") or 0) + tokens
    detail["reservation_extensions"] = int(detail.get("reservation_extensions") or 0) + 1
    detail["extended_micro_usd"] = int(detail.get("extended_micro_usd") or 0) + amount
    detail["extended_tokens"] = int(detail.get("extended_tokens") or 0) + tokens
    conn.execute(
        text(
            "UPDATE model_requests SET reserved_micro_usd = reserved_micro_usd + :amount, usage_detail = CAST(:detail AS jsonb) "
            "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"
        ),
        {
            "amount": amount,
            "detail": json.dumps(detail),
            "project_id": project_id,
            "task_id": task_id,
            "request_key": request_key,
        },
    )
    conn.execute(
        text(
            "UPDATE budget_accounts SET held_micro_usd = held_micro_usd + :amount, tokens_held = tokens_held + :tokens, "
            "version = version + 1 WHERE id = :id"
        ),
        {"amount": amount, "tokens": tokens, "id": account_id},
    )


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

    def reserve(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, amount: int, role: str, tokens: int = 1, verification: bool = False, request: dict | None = None, model_alias: str | None = None, extra_detail: dict | None = None) -> bool:
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
                if existing["request_sha256"] != digest or not _replay_matches(existing, amount):
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
                        "detail": json.dumps(_reservation_detail(tokens, extra_detail)),
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

    def settle_measured(
        self,
        *,
        owner_id: str,
        project_id: str,
        task_id: str,
        request_key: str,
        actual: int,
        input_tokens: int,
        output_tokens: int,
        unknown_tokens: int = 0,
        unknown_micro_usd: int | None = None,
        guard: SettleGuard | None = None,
    ) -> str:
        """Book known token usage and release the hold, including a measured overrun.

        A row with no measured usage at all does not belong here: that stays mark_unknown
        and keeps the hold. settle() still rejects a bill above its reservation. This path
        is for a measured coding attempt whose tokens are already known.

        actual is money (micro-USD) and input_tokens/output_tokens are tokens; they are booked
        apart and neither is derived from the other here. The caller prices the measured calls at
        the configured rate, each call rounded up on its own, and passes their sum.

        unknown_tokens is what was reserved for calls of this attempt whose usage never
        arrived (a timeout after sending, an answer without usage), and unknown_micro_usd the sum
        of those calls' own amounts at the configured rate (None: the row's rate, one amount).
        That part is charged rather than released, and only out of what is still held after the
        measured usage, each unit on its own: the estimate never pushes the bill past the hold.
        An overrun is measured money above the money held or measured tokens above the tokens held.

        guard, if given, must hold before anything is booked; otherwise fence_lost and no change.
        """
        if actual < 0 or input_tokens < 0 or output_tokens < 0 or unknown_tokens < 0 or (unknown_micro_usd is not None and unknown_micro_usd < 0):
            raise BudgetError("invalid_amount")
        measured_tokens = input_tokens + output_tokens
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _require_guard(conn, guard)
            account = conn.execute(
                text("SELECT id FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            request = conn.execute(
                text(
                    "SELECT reserved_micro_usd, usage_status, actual_micro_usd, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().one()
            detail = _detail(request["usage_detail"])
            if request["usage_status"] == "known":
                if request["actual_micro_usd"] != actual + int(detail.get("unknown_charged_micro_usd") or 0):
                    raise BudgetError("conflicting_settlement")
                return str(detail.get("book_result") or "settled")
            if request["usage_status"] == "unknown":
                raise BudgetError("unknown_not_zero")
            if request["usage_status"] != "reserved":
                raise BudgetError("not_reserved")
            reserved_cost = int(request["reserved_micro_usd"])
            reserved_tokens = int(detail.get("reserved_tokens") or 0)
            unknown_micro = coding_call_micro_usd(unknown_tokens, detail) if unknown_micro_usd is None else unknown_micro_usd
            unknown_charge_tokens, unknown_charge_micro = _unknown_charge(
                unknown_tokens,
                unknown_micro,
                measured_tokens=measured_tokens,
                measured_micro=actual,
                reserved_tokens=reserved_tokens,
                reserved_cost=reserved_cost,
            )
            overrun_micro = max(0, actual - reserved_cost)
            overrun_tokens = max(0, measured_tokens - reserved_tokens)
            book_result = "overrun" if overrun_micro or overrun_tokens else "settled"
            detail["book_result"] = book_result
            detail["pending_tokens"] = 0
            detail["reconciliation"] = "recorded" if book_result == "overrun" else "booked"
            if book_result == "overrun":
                detail["overrun_micro_usd"] = overrun_micro
                detail["overrun_tokens"] = overrun_tokens
            if unknown_tokens:
                detail["unknown_charged_tokens"] = unknown_charge_tokens
                detail["unknown_charged_micro_usd"] = unknown_charge_micro
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = 'known', actual_micro_usd = :actual, input_tokens = :input_tokens, "
                    "output_tokens = :output_tokens, usage_detail = CAST(:detail AS jsonb), completed_at = now() "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {
                    "actual": actual + unknown_charge_micro,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "detail": json.dumps(detail),
                    "project_id": project_id,
                    "task_id": task_id,
                    "request_key": request_key,
                },
            )
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = held_micro_usd - :reserved, spent_micro_usd = spent_micro_usd + :actual, "
                    "tokens_held = tokens_held - :reserved_tokens, tokens_used = tokens_used + :tokens, version = version + 1 WHERE id = :id"
                ),
                {
                    "reserved": reserved_cost,
                    "actual": actual + unknown_charge_micro,
                    "reserved_tokens": reserved_tokens,
                    "tokens": measured_tokens + unknown_charge_tokens,
                    "id": account["id"],
                },
            )
        return book_result

    def mark_unknown(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, guard: SettleGuard | None = None) -> None:
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _require_guard(conn, guard)
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

    def cancel_unused(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, guard: SettleGuard | None = None) -> bool:
        """Release a reservation through which nothing was ever sent, back to the account in full.

        Only for a row the caller has shown carried no call to a provider; True when it was released.
        """
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            _require_guard(conn, guard)
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
                return False
            _cancel_before_dispatch(
                conn, account_id=account["id"], project_id=project_id, task_id=task_id, request_key=request_key,
                amount=int(request["reserved_micro_usd"]), detail=_detail(request["usage_detail"]),
            )
        return True

    def _held_by(self, conn, *, project_id: str, task_id: str, request_key: str, attempt_id: str) -> tuple[object, dict, dict] | None:
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
            return None
        detail = _detail(request["usage_detail"])
        if detail.get("dispatch_holder") != attempt_id:
            return None
        return account["id"], dict(request), detail

    def release_unsent(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, attempt_id: str) -> None:
        """Drop this attempt's reservation if it never reached sending.

        A sending row may already be billed. It is not released here.
        """
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            held = self._held_by(conn, project_id=project_id, task_id=task_id, request_key=request_key, attempt_id=attempt_id)
            if held is None:
                return
            account_id, request, detail = held
            if detail.get("dispatch_state") not in UNSENT_DISPATCH_STATES:
                return
            _cancel_before_dispatch(
                conn, account_id=account_id, project_id=project_id, task_id=task_id, request_key=request_key,
                amount=int(request["reserved_micro_usd"]), detail=detail,
            )

    def release_refused_send(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, attempt_id: str, code: str) -> None:
        """Release a sending row the gateway refused before it contacted the provider."""
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            held = self._held_by(conn, project_id=project_id, task_id=task_id, request_key=request_key, attempt_id=attempt_id)
            if held is None:
                return
            account_id, request, detail = held
            if detail.get("dispatch_state") != "sending":
                return
            detail["gateway_refused"] = code
            _cancel_before_dispatch(
                conn, account_id=account_id, project_id=project_id, task_id=task_id, request_key=request_key,
                amount=int(request["reserved_micro_usd"]), detail=detail,
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

    def acquire_dispatch_permit(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, attempt_id: str) -> bool:
        """Mark a reserved call permitted only while the task is still runnable.

        A pause or cancel that commits after this returns can still revoke the
        permit before consume_dispatch_permit flips it to sending. An attempt
        whose reservation was taken over by a retry gets no permit.
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
            if detail.get("dispatch_state") not in UNSENT_DISPATCH_STATES or detail.get("dispatch_holder") != attempt_id:
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

    def consume_dispatch_permit(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, attempt_id: str) -> bool:
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
            if detail.get("dispatch_state") != "permitted" or detail.get("dispatch_holder") != attempt_id:
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
                _cancel_before_dispatch(
                    conn, account_id=account["id"], project_id=project_id, task_id=task_id, request_key=str(request["request_key"]),
                    amount=int(request["reserved_micro_usd"]), detail=detail,
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
        attempt_id: str,
        source_manifest: list[dict[str, str]] | None = None,
    ) -> dict[str, object]:
        """Reserve once, then either send, reuse a stored response, or stop.

        A reservation still before sending is provably unsent, so a retry takes
        it over and reprices it instead of freezing it as unknown. Only a row
        that reached sending may have been billed; that one becomes unknown.
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
                    "SELECT usage_status, reserved_micro_usd, request_sha256, usage_detail FROM model_requests "
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
                unsent = existing["usage_status"] == "reserved" and detail.get("dispatch_state") in UNSENT_DISPATCH_STATES
                if existing["usage_status"] == "cancelled_before_dispatch" or unsent:
                    previous_amount = int(existing["reserved_micro_usd"]) if unsent else 0
                    previous_tokens = int(detail.get("reserved_tokens") or 0) if unsent else 0
                    fraction = float(account["verification_reserve_fraction"])
                    ceiling = int(account["limit_micro_usd"] * (1 - fraction))
                    token_ceiling = int(account["token_limit"] * (1 - fraction))
                    fits = (
                        output_cap >= 1
                        and amount >= 1
                        and tokens >= 1
                        and account["spent_micro_usd"] + account["held_micro_usd"] - previous_amount + amount <= ceiling
                        and account["tokens_used"] + account["tokens_held"] - previous_tokens + tokens <= token_ceiling
                    )
                    if not fits:
                        if unsent:
                            _cancel_before_dispatch(
                                conn, account_id=account["id"], project_id=project_id, task_id=task_id, request_key=request_key,
                                amount=previous_amount, detail=detail,
                            )
                        return {"action": "denied"}
                    detail.update(
                        {
                            "reserved_tokens": tokens,
                            "dispatch_state": "dispatched",
                            "dispatch_holder": attempt_id,
                            "prompt_sha256": prompt_sha256,
                            "output_cap": output_cap,
                            "source_snapshot_sha256": source_snapshot_sha256,
                            "source_manifest": source_manifest or [],
                        }
                    )
                    conn.execute(
                        text(
                            "UPDATE model_requests SET usage_status = 'reserved', reserved_micro_usd = :amount, request_sha256 = :digest, usage_detail = CAST(:detail AS jsonb) "
                            "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                        ),
                        {"amount": amount, "digest": digest, "detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
                    )
                    conn.execute(
                        text(
                            "UPDATE budget_accounts SET held_micro_usd = held_micro_usd + :amount, tokens_held = tokens_held + :tokens, version = version + 1 WHERE id = :id"
                        ),
                        {"amount": amount - previous_amount, "tokens": tokens - previous_tokens, "id": account["id"]},
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
                "dispatch_holder": attempt_id,
                "prompt_sha256": prompt_sha256,
                "output_cap": output_cap,
                "source_snapshot_sha256": source_snapshot_sha256,
                "source_manifest": source_manifest or [],
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
        cost_source: str,
        pricing_version: str | None,
        finish_reason: str | None = None,
    ) -> str:
        """Store the provider response before any later artifact write.

        Known tokens are booked even when the price is missing. Missing token
        usage keeps both money and tokens held as unknown; it is not an overrun.
        A price above the reservation is kept and not settled as if it fit.
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
            elif reported_tokens is None or (tokens_fit and cost_micro_usd is None):
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
                    "cost_source": cost_source if cost_micro_usd is not None else "unreported",
                    "pricing_version": pricing_version if cost_micro_usd is not None else None,
                    "reconciliation": "unreconciled" if book_result == "overrun" else "booked",
                    "upstream_model": upstream_model,
                    "requested_alias": requested_alias,
                }
            )
            if finish_reason == "length":
                detail["finish_reason"] = "length"
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_status = :usage_status, actual_micro_usd = :actual, "
                    "input_tokens = :input_tokens, output_tokens = :output_tokens, model_revision = :upstream, pricing_version = :pricing_version, "
                    "usage_detail = CAST(:detail AS jsonb), completed_at = CASE WHEN :usage_status = 'known' THEN now() ELSE completed_at END "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {
                    "usage_status": usage_status,
                    "actual": actual,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                    "upstream": upstream_model,
                    "pricing_version": pricing_version if cost_micro_usd is not None else None,
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

    def reconcile_overrun(self, *, owner_id: str, project_id: str, task_id: str, request_key: str, decision: str) -> str:
        """Confirm or release a bill that exceeded its reservation.

        The same decision can be repeated. A later opposite decision is left as
        the one already recorded.
        """
        if decision not in {"confirm", "release"}:
            raise BudgetError("invalid_reconciliation")
        label = "confirmed" if decision == "confirm" else "released"
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT id, held_micro_usd FROM budget_accounts WHERE project_id = :project_id AND task_id = :task_id FOR UPDATE"),
                {"project_id": project_id, "task_id": task_id},
            ).mappings().one()
            request = conn.execute(
                text(
                    "SELECT reserved_micro_usd, usage_detail FROM model_requests "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key FOR UPDATE"
                ),
                {"project_id": project_id, "task_id": task_id, "request_key": request_key},
            ).mappings().one()
            detail = _detail(request["usage_detail"])
            if detail.get("book_result") != "overrun":
                return "not_overrun"
            current = str(detail.get("reconciliation") or "")
            if current in {"confirmed", "released"}:
                return current
            reserved = int(request["reserved_micro_usd"])
            reported = detail.get("reported_cost_micro_usd")
            spend = int(reported) if decision == "confirm" and reported is not None else 0
            if spend < 0:
                spend = 0
            detail["reconciliation"] = label
            conn.execute(
                text(
                    "UPDATE model_requests SET usage_detail = CAST(:detail AS jsonb) "
                    "WHERE project_id = :project_id AND task_id = :task_id AND request_key = :request_key"
                ),
                {"detail": json.dumps(detail), "project_id": project_id, "task_id": task_id, "request_key": request_key},
            )
            conn.execute(
                text(
                    "UPDATE budget_accounts SET held_micro_usd = GREATEST(held_micro_usd - :reserved, 0), "
                    "spent_micro_usd = spent_micro_usd + :spend, version = version + 1 WHERE id = :id"
                ),
                {"reserved": reserved, "spend": spend, "id": account["id"]},
            )
        return label

    def snapshot(self, *, owner_id: str, project_id: str, task_id: str, reclaimable_request_key: str | None = None) -> dict:
        """Account totals. A reclaimable request's unsent hold is left out, since a retry reprices it."""
        with self.database.transaction(owner_id=owner_id, project_id=project_id) as conn:
            account = conn.execute(
                text("SELECT limit_micro_usd, spent_micro_usd, held_micro_usd, tokens_used, tokens_held, requests_admitted FROM budget_accounts WHERE task_id = :task_id"),
                {"task_id": task_id},
            ).mappings().one()
            reclaim_amount = 0
            reclaim_tokens = 0
            if reclaimable_request_key is not None:
                pending = conn.execute(
                    text("SELECT reserved_micro_usd, usage_detail FROM model_requests WHERE task_id = :task_id AND request_key = :request_key AND usage_status = 'reserved'"),
                    {"task_id": task_id, "request_key": reclaimable_request_key},
                ).mappings().first()
                if pending is not None:
                    detail = _detail(pending["usage_detail"])
                    if detail.get("dispatch_state") in UNSENT_DISPATCH_STATES:
                        reclaim_amount = int(pending["reserved_micro_usd"])
                        reclaim_tokens = int(detail.get("reserved_tokens") or 0)
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
            "held_micro_usd": account["held_micro_usd"] - reclaim_amount,
            "unknown_request_count": int(usage["unknown_request_count"]),
            "observed_request_count": int(usage["observed_request_count"]),
            "observed_micro_usd": int(usage["observed_micro_usd"]),
            "tokens_used": account["tokens_used"],
            "tokens_held": account["tokens_held"] - reclaim_tokens,
            "request_count": account["requests_admitted"],
        }
