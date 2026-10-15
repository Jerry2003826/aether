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
    hub = run(
        [
            "python3",
            "-c",
            "import json,urllib.request; raw=urllib.request.urlopen('https://huggingface.co/api/models/BAAI/bge-m3', timeout=20).read(); meta=json.loads(raw); sha=meta.get('sha'); cfg=json.loads(urllib.request.urlopen('https://huggingface.co/BAAI/bge-m3/resolve/'+sha+'/config.json', timeout=20).read()); print(json.dumps({'sha': sha, 'hidden_size': cfg.get('hidden_size')}))",
        ],
        ROOT,
        timeout=40,
    )
    hub_meta = None
    if hub["returncode"] == 0 and hub["stdout"].strip():
        try:
            hub_meta = json.loads(hub["stdout"].strip().splitlines()[-1])
        except json.JSONDecodeError:
            hub_meta = None
    if os.environ.get("AETHER_PROBE_ML") != "1":
        return {
            "status": "blocked",
            "reason": "Hub metadata was read, but FlagEmbedding was not installed and weights were not loaded. Published hidden_size is not a measured dimension.",
            "embedding_revision": None,
            "hub_revision_unverified": None if hub_meta is None else hub_meta.get("sha"),
            "published_hidden_size_unverified": None if hub_meta is None else hub_meta.get("hidden_size"),
            "dimension_probed": None,
            "weights_present": False,
            "package_install": "not_run",
        }
    lock = run(["uv", "lock"], env, timeout=180)
    if lock["returncode"] != 0:
        return {"status": "blocked", "reason": "uv lock failed", "lock": lock, "embedding_revision": None, "dimension": None}
    sync = run(["uv", "sync", "--frozen"], env, timeout=900)
    imported = run(
        ["uv", "run", "--frozen", "python", "-c", "import FlagEmbedding, json; print(json.dumps({'file': getattr(FlagEmbedding, '__file__', None)}))"],
        env,
        timeout=180,
    )
    ok = sync["returncode"] == 0 and imported["returncode"] == 0
    if ok:
        reason = "FlagEmbedding imported, but weights were not loaded and dimension was not measured"
    else:
        reason = "FlagEmbedding install or import failed"
    return {
        "status": "blocked",
        "reason": reason,
        "import_ok": ok,
        "embedding_revision": None,
        "dimension": None,
        "weights_present": False,
        "sync": {"returncode": sync["returncode"], "stderr": sync["stderr"][-1000:]},
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
