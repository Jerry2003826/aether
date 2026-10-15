"""Write config/upstream.lock.json from the resolved uv.lock. Unresolved entries stay null."""

from __future__ import annotations

import json
import tomllib
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PACKAGE_TO_REPO = {
    "temporalio": "temporalio/sdk-python",
    "fastapi": "fastapi/fastapi",
    "pydantic": "pydantic/pydantic",
    "sqlalchemy": "sqlalchemy/sqlalchemy",
    "alembic": "sqlalchemy/alembic",
    "litellm": "BerriAI/litellm",
    "httpx": "encode/httpx",
}


def resolved_versions() -> dict[str, str]:
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    versions: dict[str, str] = {}
    for package in data.get("package", []):
        versions[package["name"]] = package["version"]
    return versions


def main() -> None:
    versions = resolved_versions()
    example = json.loads((ROOT / "config" / "upstream.lock.example.json").read_text(encoding="utf-8"))
    by_repo = {repo: versions[name] for name, repo in PACKAGE_TO_REPO.items() if name in versions}
    entries = []
    for entry in example["entries"]:
        version = by_repo.get(entry["repository"])
        entries.append(
            {
                "repository": entry["repository"],
                "role": entry["role"],
                "version": version,
                "commit_sha": None,
                "artifact_sha256": None,
                "license_files": [],
                "platforms_tested": ["linux/amd64"] if version else [],
                "probe_log": "uv.lock" if version else None,
            }
        )
    payload = {
        "is_actual_lock": False,
        "partial": True,
        "generated_by_task": "F00",
        "reason": "Only control-plane Python distributions resolved by uv are versioned. Commit SHAs, image runtime platforms, OpenHands and ML weights remain null until their own probes.",
        "python": "3.13.16",
        "entries": entries,
    }
    destination = ROOT / "config" / "upstream.lock.json"
    destination.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    locked = [entry["repository"] for entry in entries if entry["version"]]
    print(json.dumps({"locked": locked, "unlocked": [e["repository"] for e in entries if not e["version"]]}, indent=2))


if __name__ == "__main__":
    main()
