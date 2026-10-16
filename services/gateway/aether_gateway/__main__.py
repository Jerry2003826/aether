"""Model gateway process. The provider key stays in this process."""

from __future__ import annotations

import json
import os
import sys

from aether_gateway.model import GatewayDenied, ModelGateway
from aether_storage.db import Database
from aether_worker.role_http import serve_role

GATEWAY_CHAT_TIMEOUT_SECONDS = 30.0


def main() -> None:
    handed = _handoff()
    key = str(handed.get("provider_key") or "")
    worker_url = str(handed.get("worker_url") or "")
    token = os.environ.pop("AETHER_LOOPBACK_TOKEN", "")
    base = os.environ.get("AETHER_LIVE_MODEL_BASE", "").strip()
    if not worker_url or not token:
        raise SystemExit("gateway 缺少模型调用所需的交接字段或 AETHER_LOOPBACK_TOKEN")
    database = Database(worker_url)

    def health() -> dict[str, object]:
        return {"role": "gateway", "configured": True}

    def post(path: str, body: dict) -> tuple[int, dict]:
        gateway = ModelGateway(database, upstream_base=base, provider_key=key, timeout=gateway_timeout(path, body))
        if path == "/v1/chat/completions":
            try:
                result = gateway.relay_chat(str(body.get("aether_job_token") or ""), body)
            except GatewayDenied as denied:
                return 409, {"denied": True, "code": denied.code, "sent": denied.sent}
            return 200, result
        if path != "/v1/complete":
            return 404, {"error": "not_found"}
        try:
            result = gateway.complete(
                str(body.get("token") or ""),
                {"alias": body.get("alias"), "prompt": body.get("prompt"), "max_tokens": body.get("max_tokens")},
            )
        except GatewayDenied as denied:
            return 409, {"denied": True, "code": denied.code, "sent": denied.sent}
        return 200, result

    serve_role(token, health, post)


def _handoff() -> dict:
    raw = sys.stdin.readline()
    sys.stdin.close()
    try:
        loaded = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise SystemExit("gateway 交接无效") from None
    if not isinstance(loaded, dict):
        raise SystemExit("gateway 交接无效")
    return loaded


def gateway_timeout(path: str, body: dict) -> float:
    """Coding chat uses the server timeout. The request body cannot stretch it."""
    if path == "/v1/chat/completions":
        return GATEWAY_CHAT_TIMEOUT_SECONDS
    return _timeout(body.get("timeout"))


def _timeout(value: object) -> float:
    try:
        timeout = float(value) if value is not None else 30.0
    except (TypeError, ValueError):
        return 30.0
    if timeout <= 0:
        return 30.0
    return timeout


if __name__ == "__main__":
    main()
