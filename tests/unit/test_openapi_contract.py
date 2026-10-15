from pathlib import Path

import yaml

from aether_api.app import create_app
from aether_api.settings import Settings

_METHODS = {"get", "post", "put", "patch", "delete"}


def test_console_renders_text_without_raw_html() -> None:
    root = Path("apps/web")
    source = "\n".join(path.read_text(encoding="utf-8") for path in root.rglob("*.tsx") if "node_modules" not in path.parts and ".next" not in path.parts)
    assert "dangerouslySetInnerHTML" not in source
    assert "控制台需要完整显示中文长文本" in source


def test_generated_openapi_matches_the_contract_operations() -> None:
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
    expected = _operations(contract["paths"])
    actual = _operations(generated["paths"])
    assert actual == expected
    assert len(actual) == 50


def _operations(paths: dict) -> set[tuple[str, str, str]]:
    found: set[tuple[str, str, str]] = set()
    for path, methods in paths.items():
        for method, operation in methods.items():
            if method not in _METHODS:
                continue
            found.add((path, method, operation["operationId"]))
    return found
