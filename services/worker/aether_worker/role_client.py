"""Worker-side calls into the verifier and model gateway processes."""

from __future__ import annotations

from typing import Any

from aether_gateway.model import GatewayDenied
from aether_worker.loopback import LoopbackError, post_json


def verify_via_role(
    *,
    verifier_base: str,
    token: str,
    owner_id: str,
    project_id: str,
    task_id: str,
    contract_id: str,
    contract_sha: str,
    artifact_id: str,
    goal: str,
    allowed_ids: list[str] | None,
) -> str:
    if not verifier_base or not token:
        raise LoopbackError("verifier_unavailable")
    body = post_json(
        f"{verifier_base.rstrip('/')}/v1/verify",
        token,
        {
            "owner_id": owner_id,
            "project_id": project_id,
            "task_id": task_id,
            "contract_id": contract_id,
            "contract_sha": contract_sha,
            "artifact_id": artifact_id,
            "goal": goal,
            "allowed_ids": allowed_ids,
        },
        timeout=120,
    )
    verification_id = body.get("verification_id")
    if not isinstance(verification_id, str) or not verification_id:
        raise LoopbackError("verifier_rejected")
    return verification_id


def complete_via_gateway(
    *,
    gateway_base: str,
    token: str,
    call_token: str,
    alias: str,
    prompt: str,
    max_tokens: int,
    timeout: float,
) -> dict[str, Any]:
    if not gateway_base or not token:
        raise GatewayDenied("gateway_unconfigured", sent=False)
    try:
        body = post_json(
            f"{gateway_base.rstrip('/')}/v1/complete",
            token,
            {"token": call_token, "alias": alias, "prompt": prompt, "max_tokens": max_tokens, "timeout": timeout},
            timeout=timeout + 5,
        )
    except LoopbackError as exc:
        raise GatewayDenied("upstream_error", sent=True) from exc
    if body.get("denied") is True:
        raise GatewayDenied(str(body.get("code") or "gateway_denied"), sent=bool(body.get("sent")))
    return body
