"""Verifier process. It holds the verifier database role and nothing else privileged."""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

from sqlalchemy import text

from aether_storage.artifacts import ArtifactStore
from aether_storage.db import Database
from aether_worker.console_commands import apply_verifier_commands
from aether_worker.goal_effect import WorkerEndpoints, _ensure_verification
from aether_worker.role_http import BODY_REJECTED, ERROR_KEY, serve_role
from aether_worker.skill_execute import execute_pending_skill_evaluations

_OWNER_ID = "owner_id"
_PROJECT_ID = "project_id"


def main() -> None:
    handed = _handoff()
    dsn = str(handed.get("verifier_url") or "")
    token = os.environ.pop("AETHER_LOOPBACK_TOKEN", "")
    if not dsn or not token:
        raise SystemExit("verifier 缺少验收库地址或 AETHER_LOOPBACK_TOKEN")
    artifact_root = Path(os.environ.get("AETHER_ARTIFACT_ROOT", "data/artifacts"))
    provider = os.environ.get("AETHER_RESEARCH_PROVIDER", "live").strip().lower() or "live"
    endpoints = WorkerEndpoints(
        worker_url="",
        verifier_url=dsn,
        dispatcher_url="",
        artifact_root=artifact_root,
        temporal_address="",
        task_queue="",
        research_provider=provider,
    )
    database = Database(dsn)

    def health() -> dict[str, str]:
        with database.transaction() as conn:
            user = str(conn.execute(text("SELECT current_user")).scalar_one())
        return {"role": "verifier", "db_user": user}

    def post(path: str, body: dict) -> tuple[int, dict]:
        if path == "/v1/verify":
            return _verify(endpoints, body)
        if path == "/v1/commands":
            return _commands(database, body)
        if path == "/v1/skill-evaluations":
            return _evaluations(database, body)
        return 404, {ERROR_KEY: "not_found"}

    serve_role(token, health, post)


def _handoff() -> dict:
    raw = sys.stdin.readline()
    sys.stdin.close()
    try:
        loaded = json.loads(raw) if raw else {}
    except json.JSONDecodeError:
        raise SystemExit("verifier 交接无效") from None
    if not isinstance(loaded, dict):
        raise SystemExit("verifier 交接无效")
    return loaded


def _text(body: dict, name: str) -> str | None:
    value = body.get(name)
    if not isinstance(value, str) or not value:
        return None
    return value


def _verify(endpoints: WorkerEndpoints, body: dict) -> tuple[int, dict]:
    fields = {name: _text(body, name) for name in (_OWNER_ID, _PROJECT_ID, "task_id", "contract_id", "contract_sha", "artifact_id", "goal")}
    if any(value is None for value in fields.values()):
        return 400, {ERROR_KEY: BODY_REJECTED}
    allowed = body.get("allowed_ids")
    if allowed is not None and not isinstance(allowed, list):
        return 400, {ERROR_KEY: BODY_REJECTED}
    verification_id = _ensure_verification(
        endpoints,
        owner_id=str(fields[_OWNER_ID]),
        project_id=str(fields[_PROJECT_ID]),
        task_id=str(fields["task_id"]),
        contract_id=str(fields["contract_id"]),
        contract_sha=str(fields["contract_sha"]),
        artifact_id=str(fields["artifact_id"]),
        goal=str(fields["goal"]),
        allowed_ids=allowed,
    )
    return 200, {"verification_id": verification_id}


def _commands(database: Database, body: dict) -> tuple[int, dict]:
    owner_id = _text(body, _OWNER_ID)
    project_id = _text(body, _PROJECT_ID)
    if owner_id is None or project_id is None:
        return 400, {ERROR_KEY: BODY_REJECTED}
    applied = apply_verifier_commands(database, owner_id=owner_id, project_id=project_id)
    return 200, {"applied": applied}


def _evaluations(database: Database, body: dict) -> tuple[int, dict]:
    owner_id = _text(body, _OWNER_ID)
    project_id = _text(body, _PROJECT_ID)
    if owner_id is None or project_id is None:
        return 400, {ERROR_KEY: BODY_REJECTED}
    root = Path(os.environ.get("AETHER_ARTIFACT_ROOT", "data/artifacts"))
    store = ArtifactStore(root, database)
    try:
        outcomes = execute_pending_skill_evaluations(
            database,
            database,
            store,
            owner_id=owner_id,
            project_id=project_id,
            apply_verdicts=False,
        )
    except Exception:
        return 500, {ERROR_KEY: "evaluation_failed"}
    return 200, {"outcomes": outcomes}


if __name__ == "__main__":
    main()
