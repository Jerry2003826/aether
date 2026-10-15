import hashlib
import importlib.util
import json
import os
import shutil
from pathlib import Path

from aether_worker.f26_acceptance import (
    LIVE_ALIAS,
    LIVE_BASE,
    LIVE_KEY,
    MAX_CALLS,
    MAX_OUTPUT_TOKENS,
    MAX_TOTAL_TOKENS,
    QUERY,
    SNAPSHOT,
    _coding_prompt,
    _copy_snapshot,
    _forward_body,
    _hybrid,
    _local_check,
    _read_sources,
    _report_cites,
    run_f26_acceptance,
)

_RECORDED = Path(__file__).resolve().parents[2] / "reports" / "f26_acceptance_result.json"
_RECORDED_REPORT_SHA = "db93defd5af32d96328eb28baf572736f2d83f2521dd9a9c354284c4ade62bcf"


def _encode(texts: list[str]) -> list[list[float]]:
    assert texts[0] == QUERY
    assert len(texts) == 4
    return [[1.0, 0.0], [0.9, 0.1], [0.8, 0.2], [0.0, 1.0]]


def _verify(tree: Path) -> dict[str, object]:
    source = (tree / "billing.py").read_text(encoding="utf-8")
    passed = "return a + b" in source
    stdout = "add(1, 2)=3\nPASS\n" if passed else "add(1, 2)=-1\nFAIL\n"
    return {
        "exit_code": 0 if passed else 1,
        "stdout": stdout,
        "passed": passed,
        "tree_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "runner": "docker",
        "image": "python:3.12-alpine",
        "flags": ["--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "32"],
    }


def _protocol() -> dict[str, object]:
    return {
        "id": "chatcmpl-protocol",
        "model": "deepseek-chat",
        "choices": [
            {
                "message": {
                    "tool_calls": [
                        {"function": {"name": "cite_finding", "arguments": json.dumps({"claim": "pong", "citation_id": "none"})}}
                    ]
                }
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
    }


def _isolate(secret: str) -> dict[str, object]:
    assert secret
    return {
        "status": "pass",
        "image": "python:3.12-alpine",
        "flags": ["--network", "none", "--read-only", "--cap-drop", "ALL", "--pids-limit", "32"],
        "exit_code": 0,
        "network_errno": 101,
        "secret_present": False,
        "reason": None,
    }


def _comparison() -> str:
    parts = ["两点都写沸点是 100。"]
    for item in _read_sources():
        if item["source_id"] == "src-piano":
            continue
        parts.append(
            f"{item['source_id']} version {item['version']} sha256 {item['content_sha256']} "
            f"offset {item['start_offset']}-{item['end_offset']}"
        )
    return " ".join(parts)


def _env(monkeypatch) -> None:
    monkeypatch.setenv(LIVE_KEY, "sk-live-SECRET")
    monkeypatch.setenv(LIVE_BASE, "http://127.0.0.1:9/v1")
    monkeypatch.setenv(LIVE_ALIAS, "deepseek-chat")


def test_caps_stay_under_one_dollar() -> None:
    assert MAX_CALLS == 8
    assert MAX_TOTAL_TOKENS == 20000
    assert MAX_OUTPUT_TOKENS == 512


def test_authorized_snapshot_fails_before_any_patch() -> None:
    destination = Path(os.environ.get("TMPDIR", "/tmp")) / "aether-f26-snapshot-check"
    _copy_snapshot(destination)
    try:
        result = _local_check(destination)
    finally:
        shutil.rmtree(destination, ignore_errors=True)
    assert result["passed"] is False
    assert "add(1, 2)=-1" in result["stdout"]
    prompt = _coding_prompt(result["stdout"], (SNAPSHOT / "billing.py").read_text(encoding="utf-8"))
    assert "请改成 return a + b" not in prompt
    assert "return a - b" in prompt


def test_failed_acceptance_is_not_reused_and_key_stays_out_of_the_job(monkeypatch) -> None:
    _env(monkeypatch)
    original = (SNAPSHOT / "billing.py").read_bytes()
    seen = {"n": 0}

    def launch(prompt: str, env: dict[str, str]) -> str:
        assert "sk-live-SECRET" not in env.values()
        assert env["AETHER_JOB_TOKEN"] != "sk-live-SECRET"
        assert "请改成 return a + b" not in prompt
        if "比较" in prompt:
            assert "AETHER_ATTEMPT_DIR" not in env
            return _comparison()
        seen["n"] += 1
        path = Path(env["AETHER_ATTEMPT_DIR"]) / "billing.py"
        assert "add(1, 2)=-1" in prompt
        if seen["n"] == 1:
            path.write_bytes(original)
            return original.decode()
        path.write_text("def add(a, b):\n    return a + b\n", encoding="utf-8")
        return "def add(a, b):\n    return a + b\n"

    report = run_f26_acceptance(encode=_encode, verify=_verify, launch=launch, protocol=_protocol, isolate=_isolate)
    encoded = json.dumps(report)
    assert "sk-live-SECRET" not in encoded
    assert report["live_ready"] is False
    assert report["scripted_substitute"] is True
    assert report["coding"]["status"] == "pass"
    assert report["coding"]["baseline"]["accepted"] is False
    assert report["coding"]["attempts"][0]["accepted"] is False
    assert report["coding"]["attempts"][0]["patch_writer"] == "unchanged"
    assert report["coding"]["attempts"][0]["reused_failed_tree"] is False
    assert report["coding"]["attempts"][1]["accepted"] is True
    assert report["coding"]["attempts"][1]["patch_writer"] == "openhands"
    assert report["coding"]["attempts"][1]["exit_code"] == 0
    assert report["coding"]["attempts"][1]["runner"] == "docker"
    assert "--network" in report["coding"]["attempts"][1]["flags"]
    assert report["coding"]["attempts"][0]["pre_patch_tree_sha256"] == report["coding"]["snapshot_sha256"]
    assert report["coding"]["attempts"][1]["pre_patch_tree_sha256"] == report["coding"]["snapshot_sha256"]
    assert report["coding"]["attempts"][0]["tree_sha256"] != report["coding"]["attempts"][1]["tree_sha256"]
    assert report["research"]["status"] == "pass"
    assert report["research"]["encoder"] == "injected"
    assert report["research"]["embedding_revision"] is None
    assert report["research"]["semantic_support"] == "not_judged"
    assert report["research"]["ranks"]["src-zh"] < report["research"]["ranks"]["src-piano"]
    assert report["research"]["ranks"]["src-en"] < report["research"]["ranks"]["src-piano"]
    assert {item["source_id"]: item["version"] for item in report["research"]["sources"]} == {
        "src-zh": 1,
        "src-en": 1,
        "src-piano": 1,
    }
    assert report["protocol"]["status"] == "pass"
    assert report["linux_isolation"]["exit_code"] == 0
    assert report["linux_isolation"]["network_errno"] == 101
    assert report["linux_isolation"]["secret_present"] is False
    assert [item["kind"] for item in report["calls_detail"]] == ["protocol", "coding", "coding", "research"]
    assert report["status"] == "probed"
    assert report["caps"]["spend_cap_usd"] == 1


def test_harness_does_not_apply_the_model_text_itself(monkeypatch) -> None:
    _env(monkeypatch)

    def launch(prompt: str, env: dict[str, str]) -> str:
        if "比较" in prompt:
            return _comparison()
        assert "AETHER_ATTEMPT_DIR" in env
        return '{"billing_py": "def add(a, b):\\n    return a + b\\n"}'

    report = run_f26_acceptance(encode=_encode, verify=_verify, launch=launch, protocol=_protocol, isolate=_isolate)
    assert report["coding"]["status"] == "fail"
    assert all(item["patch_writer"] == "unchanged" for item in report["coding"]["attempts"])
    assert all(item["accepted"] is False for item in report["coding"]["attempts"])
    assert report["status"] == "failed"
    assert report["live_ready"] is False


def test_missing_docker_does_not_spend_a_coding_call(monkeypatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr("aether_worker.f26_acceptance.shutil.which", lambda _name: None)

    def launch(prompt: str, env: dict[str, str]) -> str:
        assert "比较" in prompt
        assert "AETHER_ATTEMPT_DIR" not in env
        return _comparison()

    report = run_f26_acceptance(encode=_encode, launch=launch, protocol=_protocol, isolate=_isolate)
    assert report["coding"]["status"] == "not_run"
    assert report["coding"]["reason"] == "docker_missing"
    assert report["coding"]["attempts"] == []
    assert report["status"] == "incomplete"
    assert report["live_ready"] is False


def test_fixture_vectors_are_not_a_live_encoder(monkeypatch) -> None:
    monkeypatch.setattr("aether_worker.f26_acceptance._encoder_mode", lambda _texts: "fixture")
    result = _hybrid(_read_sources(), None)
    assert result["status"] == "not_run"
    assert result["reason"] == "fixture_vectors_not_live"
    assert result["encoder"] == "fixture"


def test_gateway_drops_extra_fields_and_rejects_other_models() -> None:
    raw, dropped = _forward_body(
        json.dumps(
            {
                "model": "openai/deepseek-chat",
                "messages": [{"role": "user", "content": "ping"}],
                "max_completion_tokens": 50,
                "reasoning_effort": "low",
            }
        ).encode(),
        "deepseek-chat",
    )
    body = json.loads(raw)
    assert body["model"] == "deepseek-chat"
    assert body["max_tokens"] == 50
    assert "reasoning_effort" not in body
    assert dropped == ["reasoning_effort"]
    try:
        _forward_body(b'{"model":"gpt-4","messages":[]}', "deepseek-chat")
    except RuntimeError as exc:
        assert str(exc) == "model_rejected"
    else:
        raise AssertionError("other models must be rejected")


def test_secret_in_an_executor_error_is_redacted(monkeypatch) -> None:
    _env(monkeypatch)

    def launch(prompt: str, env: dict[str, str]) -> str:
        raise RuntimeError(f"boom {env.get('AETHER_JOB_TOKEN', '')} sk-live-SECRET")

    report = run_f26_acceptance(encode=_encode, verify=_verify, launch=launch, protocol=_protocol, isolate=_isolate)
    encoded = json.dumps(report)
    assert "sk-live-SECRET" not in encoded
    assert "[REDACTED]" in encoded
    assert report["status"] == "failed"
    assert report["live_ready"] is False


def test_dac5c49_report_cites_both_sources_with_equals_signs() -> None:
    payload = json.loads(_RECORDED.read_text(encoding="utf-8"))
    report = payload["research"]["report"]
    sources = payload["research"]["sources"]
    assert hashlib.sha256(report.encode()).hexdigest() == _RECORDED_REPORT_SHA
    assert payload["status"] == "failed"
    assert payload["protocol"]["status"] == "pass"
    assert payload["coding"]["status"] == "pass"
    assert payload["coding"]["baseline"]["stdout"].startswith("add(1, 2)=-1")
    assert payload["coding"]["attempts"][0]["stdout"].endswith("PASS\n")
    assert payload["linux_isolation"]["status"] == "pass"
    assert payload["research"]["reason"] == "citation_mismatch"
    assert payload["research"]["citations_cover_sources"] is False
    assert payload["calls_detail"][2]["completion_tokens"] == 512
    assert payload["caps"]["max_output_tokens"] == 512
    assert report.endswith("src-en")
    assert "version 1" not in report
    assert "offset 0-92" not in report
    assert "version=1" in report
    assert "offset=0-92" in report
    assert "offset=0-283" in report
    assert _report_cites(report, sources) is True
    english = sources[1]["content_sha256"]
    assert _report_cites(report.replace(english, "0" * 64), sources) is False
    assert _report_cites(report.replace("offset=0-283", "offset=0-999"), sources) is False
    assert _report_cites(report.replace("version=1", "version=9"), sources) is False
    assert _report_cites(report.replace("100", "九十"), sources) is False


def test_missing_key_is_blocked(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    report = run_f26_acceptance()
    assert report["status"] == "blocked"
    assert report["live_ready"] is False
    assert report["scripted_substitute"] is False


def test_script_writes_blocked_result_without_calling_out(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    path = Path("scripts/run_f26_acceptance.py")
    spec = importlib.util.spec_from_file_location("run_f26_acceptance", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    destination = tmp_path / "f26_acceptance_result.json"
    module.RESULT_PATH = destination
    try:
        module.main(["--out", "reports/somewhere-else.json"])
    except SystemExit as exc:
        assert str(exc) == "out must be reports/f26_acceptance_result.json"
    else:
        raise AssertionError("other output paths must be rejected")
    before = _RECORDED.read_bytes()
    code = module.main(["--out", "reports/f26_acceptance_result.json"])
    written = json.loads(destination.read_text(encoding="utf-8"))
    assert code == 2
    assert written["status"] == "blocked"
    assert written["live_ready"] is False
    assert _RECORDED.read_bytes() == before
