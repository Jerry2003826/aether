"""Probe OpenHands and ML environments. Writes honest status; does not invent revisions."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def run(cmd: list[str], cwd: Path, timeout: int) -> dict:
    try:
        completed = subprocess.run(cmd, cwd=cwd, text=True, capture_output=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired as exc:
        return {"returncode": None, "timed_out": True, "stdout": (exc.stdout or "")[-4000:], "stderr": (exc.stderr or "")[-4000:]}
    return {
        "returncode": completed.returncode,
        "timed_out": False,
        "stdout": completed.stdout[-4000:],
        "stderr": completed.stderr[-4000:],
    }


def probe_openhands() -> dict:
    env = ROOT / "environments" / "openhands"
    lock = run(["uv", "lock"], env, timeout=180)
    if lock["returncode"] != 0:
        return {"status": "blocked", "reason": "uv lock failed for the pinned OpenHands set", "lock": lock, "imports": None}
    sync = run(["uv", "sync", "--frozen"], env, timeout=600)
    if sync["returncode"] != 0:
        return {"status": "blocked", "reason": "uv sync failed", "lock": {"returncode": 0}, "sync": sync, "imports": None}
    imported = run(["uv", "run", "--frozen", "python", "probe_import.py"], env, timeout=180)
    payload = None
    if imported["returncode"] == 0 and imported["stdout"].strip():
        try:
            payload = json.loads(imported["stdout"].strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = None
    modules = None if payload is None else payload.get("modules")
    gateway_ok = bool(payload) and payload.get("llm_base_url") == "http://127.0.0.1:9/v1"
    ok = bool(modules) and all(item.get("imported") for item in modules.values()) and gateway_ok
    return {
        "status": openhands_probe_status(imports_ok=ok, model_called=False),
        "reason": None if ok else "OpenHands import or LLM base_url configuration failed",
        "selected_release": "1.54.0",
        "selection": "PyPI current versions of the four packages matched 1.54.0 on the probe date; not a hand-written digest",
        "interpreter": "CPython 3.13.16 via uv",
        "imports": modules,
        "llm_gateway_configured": gateway_ok,
        "llm_gateway_note": "LLM(base_url) accepted a local gateway URL. No provider was called.",
        "sync_returncode": sync["returncode"],
        "live_model": "not_run",
        "stderr_tail": None if ok else imported["stderr"][-1000:],
    }


def probe_ml() -> dict:
    env = ROOT / "environments" / "ml"
    if os.environ.get("AETHER_PROBE_ML") != "1":
        return {
            "status": "blocked",
            "reason": "Set AETHER_PROBE_ML=1 to load BAAI/bge-m3 and encode on CPU. Published hidden_size is not a measured dimension.",
            "embedding_revision": None,
            "dimension_probed": None,
            "weights_present": False,
            "package_install": "not_run",
            "device": None,
        }
    sync = run(["uv", "sync", "--frozen"], env, timeout=900)
    encoded = run(["uv", "run", "--frozen", "python", "probe_encode.py"], env, timeout=1800)
    payload = None
    if encoded["returncode"] == 0 and encoded["stdout"].strip():
        try:
            payload = json.loads(encoded["stdout"].strip().splitlines()[-1])
        except json.JSONDecodeError:
            payload = None
    dimension = None if payload is None else payload.get("dimension_probed")
    revision = None if payload is None else payload.get("embedding_revision")
    measured = isinstance(dimension, int) and dimension > 0 and isinstance(revision, str) and len(revision) == 40
    return {
        "status": "passed" if measured and sync["returncode"] == 0 else "blocked",
        "reason": None if measured else "FlagEmbedding encode did not return a measured dimension and revision",
        "embedding_revision": revision,
        "dimension_probed": dimension,
        "weights_present": bool(payload and payload.get("weights_present")),
        "package_install": "uv sync --frozen",
        "device": None if payload is None else payload.get("device"),
        "sample_norm": None if payload is None else payload.get("sample_norm"),
        "sync_returncode": sync["returncode"],
        "stderr_tail": None if measured else encoded["stderr"][-1000:],
    }


def openhands_probe_status(*, imports_ok: bool, model_called: bool) -> str:
    """Import success is not a model-call pass."""
    if not imports_ok:
        return "blocked"
    if not model_called:
        return "partial"
    return "passed"


def main() -> int:
    report = {"openhands": probe_openhands(), "ml": probe_ml()}
    destination = ROOT / "reports" / "f01-probe.json"
    destination.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))
    openhands = report["openhands"]
    if openhands["status"] == "passed" and openhands.get("live_model") == "not_run":
        return 1
    if openhands["status"] == "passed" and openhands.get("imports") is None:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
