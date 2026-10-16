"""evaluateSkill and createCodingJob stay accepted commands with the published contract."""

from pathlib import Path

import yaml
from fastapi.routing import APIRoute

from aether_api.app import create_app
from aether_api.settings import Settings

_PATHS = (
    "/v1/projects/{project_id}/skills/{skill_id}/evaluate",
    "/v1/projects/{project_id}/coding-jobs",
)
_OPERATIONS = {"evaluateSkill", "createCodingJob"}


def test_coding_and_skill_routes_stay_accepted_commands() -> None:
    contract = yaml.safe_load(Path("contracts/openapi.yaml").read_text(encoding="utf-8"))
    app = create_app(
        Settings(
            database_url=None,
            bootstrap_path=Path("secrets/bootstrap.json"),
            allow_testclient=True,
            origins=frozenset({"http://127.0.0.1:3000"}),
            cookie_secure=False,
        )
    )
    generated = app.openapi()
    for spec in (contract, generated):
        for path in _PATHS:
            operation = spec["paths"][path]["post"]
            assert "202" in operation["responses"], path
            assert "422" in operation["responses"], path
    declared = {
        route.operation_id: route.status_code
        for route in app.routes
        if isinstance(route, APIRoute) and route.operation_id in _OPERATIONS
    }
    assert declared == {"evaluateSkill": 202, "createCodingJob": 202}
    for path in _PATHS:
        operation = contract["paths"][path]["post"]
        required = {item["name"] for item in operation.get("parameters", []) if item.get("required")}
        assert "Idempotency-Key" in required, path
        csrf = next(item for item in operation["parameters"] if item["name"] == "X-CSRF-Token")
        assert csrf["required"] is True
