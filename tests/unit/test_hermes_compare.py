import json
import subprocess
import threading
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import pytest

from aether_worker.hermes_compare import (
    CASE_ID,
    EMPTY_TOOLSET,
    EVENT_STREAM,
    LESSON,
    LIVE_ALIAS,
    LIVE_BASE,
    LIVE_KEY,
    MAX_CALLS,
    MAX_OUTPUT_TOKENS,
    MAX_TOTAL_TOKENS,
    MODEL_NAME,
    WALL_SECONDS,
    _Gateway,
    _child_env,
    _default_hermes,
    _estimate_tokens,
    _hermes_argv,
    _invoke_hermes,
    _post_gateway,
    _write_home,
    grade_answer,
    hermes_identity,
    prompt_for,
    run_four_way,
)


def _env(monkeypatch) -> None:
    monkeypatch.setenv(LIVE_KEY, "sk-compare-SECRET")
    monkeypatch.setenv(LIVE_BASE, "https://api.deepseek.com/v1")
    monkeypatch.setenv(LIVE_ALIAS, "deepseek-chat")
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgres://leak")
    monkeypatch.setenv("HOME", "/home/ubuntu")


def test_caps_stay_under_one_dollar() -> None:
    assert MAX_CALLS == 8
    assert MAX_TOTAL_TOKENS == 8000
    assert MAX_OUTPUT_TOKENS == 128
    assert 8000 * 10 / 1_000_000 < 1


def test_grade_accepts_only_the_integer_sum() -> None:
    assert grade_answer('{"answer": 3}')["success"] is True
    wrong = grade_answer('{"answer": 4}')
    assert wrong["success"] is False
    assert wrong["false_completion"] is True
    assert grade_answer("我不知道")["abstained"] is True
    assert grade_answer("我不知道")["false_completion"] is False
    wrapped = grade_answer('说明 {"answer": 3}')
    assert wrapped["success"] is False
    assert wrapped["abstained"] is True
    fenced = grade_answer('```json\n{"answer": 3}\n```')
    assert fenced["success"] is False
    assert fenced["abstained"] is True


def test_missing_key_is_blocked(monkeypatch) -> None:
    monkeypatch.delenv(LIVE_KEY, raising=False)
    monkeypatch.delenv(LIVE_BASE, raising=False)
    monkeypatch.delenv(LIVE_ALIAS, raising=False)
    report = run_four_way()
    assert report["status"] == "blocked"
    assert report["comparative_evidence"] == "not_run"
    assert report["superior"] is None
    assert report["calls"] == 0


def test_four_groups_share_the_task_and_do_not_declare_a_winner(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    seen: list[str] = []

    def launch(prompt: str, env: dict[str, str], home: Path) -> str:
        assert "sk-compare-SECRET" not in env.values()
        assert env["OPENAI_API_KEY"] != "sk-compare-SECRET"
        assert env["HOME"] == str(home)
        assert env["HERMES_HOME"] == str(home)
        assert "AETHER_DATABASE_URL" not in env
        assert "DEV-F00" not in prompt
        seen.append(prompt)
        return '{"answer": 3}'

    def complete(prompt: str) -> str:
        seen.append(prompt)
        return '{"answer": 4}' if LESSON in prompt else '{"answer": 3}'

    report = run_four_way(launch=launch, complete=complete)
    encoded = json.dumps(report)
    assert "sk-compare-SECRET" not in encoded
    assert report["comparative_evidence"] == "not_run"
    assert report["superior"] is None
    assert report["summary"]["superior"] is None
    assert report["scripted_substitute"] is True
    assert report["dev"]["executed_case_id"] == CASE_ID
    assert report["dev"]["dev_file_cases_sent"] is False
    assert report["holdout"]["status"] == "not_run"
    assert seen[0] == prompt_for(False)
    assert seen[1] == prompt_for(True)
    assert LESSON not in seen[0]
    assert LESSON not in seen[2]
    assert LESSON in seen[1]
    assert LESSON in seen[3]
    assert report["groups"]["hermes_cold"]["success"] is True
    assert report["groups"]["aether_learned"]["success"] is False
    assert report["groups"]["aether_learned"]["false_completion"] is True
    assert report["groups"]["hermes_cold"]["cost_status"] == "unknown"
    assert report["summary"]["groups"]["hermes_cold"]["cost_mean_micro_usd"] is None
    assert report["status"] == "probed"
    assert len(seen) == 4
    argv = _hermes_argv(Path("hermes"), prompt_for(False))
    assert "--oneshot" in argv
    assert "--max-turns" in argv
    assert "1" in argv
    assert "--ignore-rules" not in argv
    assert "--toolsets" in argv
    assert EMPTY_TOOLSET in argv
    assert argv[argv.index("--run-budget") + 1] == str(WALL_SECONDS)
    assert "sk-compare-SECRET" not in argv
    assert report["hermes_identity"]["status"] == "not_checked"
    assert report["comparative_evidence"] == "not_run"


def test_cap_leaves_later_groups_unrun(monkeypatch) -> None:
    _env(monkeypatch)
    monkeypatch.setattr("aether_worker.hermes_compare.MAX_CALLS", 2)

    def launch(prompt: str, env: dict[str, str], home: Path) -> str:
        return '{"answer": 3}'

    def complete(prompt: str) -> str:
        return '{"answer": 3}'

    report = run_four_way(launch=launch, complete=complete)
    assert report["groups"]["hermes_cold"]["status"] == "ran"
    assert report["groups"]["hermes_learned"]["status"] == "ran"
    assert report["groups"]["aether_cold"]["status"] == "not_run"
    assert report["groups"]["aether_learned"]["reason"] == "cap"
    assert report["status"] == "incomplete"
    assert report["superior"] is None


def test_holdout_inside_the_repo_is_rejected(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    repo = tmp_path / "repo"
    repo.mkdir()
    dev = repo / "dev.jsonl"
    dev.write_text('{"case_id": "DEV-1", "split": "public_development"}\n', encoding="utf-8")
    inside = repo / "holdout.jsonl"
    inside.write_text('{"split": "holdout"}\n', encoding="utf-8")
    report = run_four_way(dev_cases=dev, repo_root=repo, holdout=inside, launch=lambda *_args: '{"answer": 3}', complete=lambda _prompt: '{"answer": 3}')
    assert report["status"] == "failed"
    assert report["reason"] == "holdout_inside_worktree"
    assert report["calls"] == 0
    assert report["comparative_evidence"] == "not_run"


def test_learned_home_writes_memory_and_disables_host_tools(tmp_path: Path) -> None:
    home = tmp_path / "home"
    _write_home(home, "http://127.0.0.1:9/v1", "job-token", learned=True)
    memory = (home / "memories" / "MEMORY.md").read_text(encoding="utf-8")
    config = (home / "config.yaml").read_text(encoding="utf-8")
    assert memory.strip() == LESSON
    assert "platform_toolsets:" in config
    assert "cli: []" in config
    assert "memory_enabled: true" in config
    assert "terminal" in config
    cold = tmp_path / "cold"
    _write_home(cold, "http://127.0.0.1:9/v1", "job-token", learned=False)
    assert not (cold / "memories" / "MEMORY.md").exists()


def test_child_env_drops_unrelated_secrets(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    monkeypatch.setenv("PATH", "/usr/bin:sk-compare-SECRET")
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "aws-secret")
    home = tmp_path / "isolated"
    env = _child_env(home, "http://127.0.0.1:9/v1", "job-token", "sk-compare-SECRET")
    assert env["HOME"] == str(home)
    assert env["HERMES_HOME"] == str(home)
    assert env["OPENAI_API_KEY"] == "job-token"
    assert "PATH" not in env
    assert "AWS_SECRET_ACCESS_KEY" not in env
    assert LIVE_KEY not in env
    assert "sk-compare-SECRET" not in env.values()


def test_wall_clock_is_shared(monkeypatch) -> None:
    seen: dict[str, object] = {}

    def run(argv, **kwargs):
        seen["timeout"] = kwargs.get("timeout")
        return subprocess.CompletedProcess(argv, 0, stdout='{"answer": 3}', stderr="")

    monkeypatch.setattr("aether_worker.hermes_compare.subprocess.run", run)

    class _Response:
        def read(self) -> bytes:
            return b'{"choices":[{"message":{"content":"{\\"answer\\": 3}"}}]}'

        def __enter__(self):
            return self

        def __exit__(self, *_args) -> bool:
            return False

    def open_request(self, request, timeout=None):
        del self, request
        seen["http_timeout"] = timeout
        return _Response()

    monkeypatch.setattr(urllib.request.OpenerDirector, "open", open_request)
    assert _default_hermes(Path("hermes"), "hi", {}, Path(".")) == '{"answer": 3}'
    assert _post_gateway("http://127.0.0.1/v1", {"model": MODEL_NAME, "messages": []}, "tok", "")
    assert seen["timeout"] == WALL_SECONDS
    assert seen["http_timeout"] == WALL_SECONDS


def test_identity_mismatch_does_not_spend(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    binary = tmp_path / "hermes"
    binary.write_text("not-hermes", encoding="utf-8")
    monkeypatch.setattr("aether_worker.hermes_compare.HERMES_BIN", binary)
    monkeypatch.setattr(
        "aether_worker.hermes_compare.hermes_identity",
        lambda _binary: {"status": "mismatch", "matches": False, "commit": "deadbeef", "expected_commit": "297552a9", "version_line": "nope", "path": str(_binary)},
    )
    report = run_four_way()
    assert report["status"] == "incomplete"
    assert report["reason"] == "hermes_identity_mismatch"
    assert report["calls"] == 0
    assert report["comparative_evidence"] == "not_run"
    assert report["hermes_identity"]["matches"] is False


def test_identity_probe_uses_isolated_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv(LIVE_KEY, "sk-compare-SECRET")
    monkeypatch.setenv("AETHER_DATABASE_URL", "postgres://leak")
    monkeypatch.setenv("HOME", "/home/ubuntu")
    seen: list[dict[str, str]] = []

    def run(argv, **kwargs):
        seen.append(kwargs.get("env") or {})
        if argv[0] == "git":
            return subprocess.CompletedProcess(argv, 0, stdout="297552a9d646119871391d202509822cd86330df\n", stderr="")
        return subprocess.CompletedProcess(argv, 0, stdout="Hermes Agent v0.22.1 (297552a9)\n", stderr="")

    binary = tmp_path / ".hermes" / ".venv" / "bin" / "hermes"
    binary.parent.mkdir(parents=True)
    binary.write_text("#!/bin/sh\n", encoding="utf-8")
    row = hermes_identity(binary, run=run)
    assert row["matches"] is True
    assert row["status"] == "verified"
    assert seen
    for env in seen:
        assert env["HOME"] == env["HERMES_HOME"]
        assert env["HOME"] != "/home/ubuntu"
        assert "sk-compare-SECRET" not in env.values()
        assert "AETHER_DATABASE_URL" not in env


def test_failed_hermes_keeps_metered_calls(tmp_path: Path, monkeypatch) -> None:
    recorded: list[dict] = []

    def boom(_binary, _prompt, _env, _work) -> str:
        recorded.append(
            {
                "id": "call-1",
                "total_tokens": 21,
                "held_tokens": 21,
                "source": "gateway",
                "cost_status": "unknown",
                "model": "deepseek-chat-v3",
                "requested_model": MODEL_NAME,
            }
        )
        raise RuntimeError("exit 2")

    monkeypatch.setattr("aether_worker.hermes_compare._default_hermes", boom)
    gateway = type("Gateway", (), {"calls": recorded})()
    calls: list[dict] = []
    with pytest.raises(RuntimeError, match="exit 2"):
        _invoke_hermes(
            "hermes_cold",
            prompt_for(False),
            learned=False,
            home=tmp_path / "home",
            work=tmp_path / "home" / "work",
            gateway=gateway,
            gateway_url="http://127.0.0.1:9/v1",
            token="job-token",
            secret="sk-compare-SECRET",
            binary=Path("hermes"),
            launch=None,
            calls=calls,
        )
    assert calls[0]["id"] == "call-1"
    assert calls[0]["group"] == "hermes_cold"
    assert calls[0]["total_tokens"] == 21
    assert calls[0]["model"] == "deepseek-chat-v3"


def test_report_keeps_calls_when_the_process_fails(tmp_path: Path, monkeypatch) -> None:
    _env(monkeypatch)
    binary = tmp_path / "hermes"
    binary.write_text("x", encoding="utf-8")
    shared: list[dict] = []
    monkeypatch.setattr("aether_worker.hermes_compare.HERMES_BIN", binary)
    monkeypatch.setattr(
        "aether_worker.hermes_compare.hermes_identity",
        lambda _binary: {"status": "verified", "matches": True, "commit": "297552a9d646119871391d202509822cd86330df", "expected_commit": "297552a9d646119871391d202509822cd86330df", "version": "0.22.1", "version_line": "Hermes Agent v0.22.1 (297552a9)", "path": str(_binary)},
    )

    class _FakeGateway:
        def __init__(self, *_args, **_kwargs) -> None:
            self.calls = shared
            self.server_address = ("127.0.0.1", 9)

        def serve_forever(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def server_close(self) -> None:
            return None

        def allow(self) -> bool:
            return True

    def boom(*_args, **_kwargs) -> str:
        shared.append({"id": f"call-{len(shared) + 1}", "total_tokens": 21, "held_tokens": 21, "source": "gateway", "cost_status": "unknown", "model": "deepseek-chat-v3", "requested_model": MODEL_NAME})
        raise RuntimeError("exit 2")

    monkeypatch.setattr("aether_worker.hermes_compare._Gateway", _FakeGateway)
    monkeypatch.setattr("aether_worker.hermes_compare._default_hermes", boom)
    report = run_four_way()
    assert report["comparative_evidence"] == "not_run"
    assert report["superior"] is None
    assert report["calls"] >= 1
    assert report["calls_detail"][0]["total_tokens"] == 21
    assert report["calls_detail"][0]["model"] == "deepseek-chat-v3"
    assert report["groups"]["hermes_cold"]["status"] == "failed"
    assert report["groups"]["hermes_cold"]["call_ids"] == ["call-1"]
    assert report["groups"]["hermes_cold"]["held_tokens"] == 21
    assert "sk-compare-SECRET" not in json.dumps(report)


class _Upstream(ThreadingHTTPServer):
    def __init__(self) -> None:
        self.bodies: list[bytes] = []
        super().__init__(("127.0.0.1", 0), _UpstreamHandler)


class _UpstreamHandler(BaseHTTPRequestHandler):
    def do_POST(self) -> None:  # noqa: N802
        server: _Upstream = self.server  # type: ignore[assignment]
        raw = self.rfile.read(int(self.headers.get("Content-Length", "0")))
        server.bodies.append(raw)
        payload = json.loads(raw)
        if payload.get("stream") is True:
            body = (
                b'data: {"id":"up-1","model":"deepseek-chat-v3","choices":[{"index":0,"delta":{"content":"{\\"answer\\": 3}"}}]}\n\n'
                b'data: {"id":"up-1","model":"deepseek-chat-v3","choices":[{"index":0,"delta":{},"finish_reason":"stop"}],"usage":{"prompt_tokens":4,"completion_tokens":2,"total_tokens":6}}\n\n'
                b"data: [DONE]\n\n"
            )
            content_type = EVENT_STREAM
        else:
            body = b'{"id":"up-2","model":"deepseek-chat-v3","choices":[{"message":{"content":"{\\"answer\\": 3}"}}],"usage":{"prompt_tokens":3,"completion_tokens":2,"total_tokens":5}}'
            content_type = "application/json"
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt: str, *_args: object) -> None:
        return


def _start(server: ThreadingHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread


def _stop(server: ThreadingHTTPServer, thread: threading.Thread) -> None:
    server.shutdown()
    server.server_close()
    thread.join(timeout=2)


def test_gateway_preserves_stream_and_reserves_tokens(monkeypatch) -> None:
    upstream = _Upstream()
    upstream_thread = _start(upstream)
    gateway = _Gateway(f"http://127.0.0.1:{upstream.server_address[1]}/", "provider-SECRET", MODEL_NAME)
    gateway_thread = _start(gateway)
    monkeypatch.setenv("AETHER_GATEWAY_TOKEN", "job-token")
    try:
        stream_body = json.dumps(
            {
                "model": MODEL_NAME,
                "messages": [{"role": "user", "content": "hi"}],
                "stream": True,
                "stream_options": {"include_usage": True},
                "tools": [{"type": "function", "function": {"name": "terminal"}}],
            }
        ).encode()
        request = urllib.request.Request(
            f"http://127.0.0.1:{gateway.server_address[1]}/v1/chat/completions",
            data=stream_body,
            headers={"Authorization": "Bearer job-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(request, timeout=5) as response:
            streamed = response.read()
            stream_type = response.headers.get("Content-Type", "")
        assert EVENT_STREAM in stream_type
        assert b"data:" in streamed
        forwarded = json.loads(upstream.bodies[0])
        assert forwarded["stream"] is True
        assert forwarded["stream_options"] == {"include_usage": True}
        assert "tools" not in forwarded
        assert gateway.calls[0]["model"] == "deepseek-chat-v3"
        assert gateway.calls[0]["requested_model"] == MODEL_NAME
        assert gateway.calls[0]["total_tokens"] == 6
        assert "stream" not in gateway.calls[0]["dropped_fields"]
        assert "tools" in gateway.calls[0]["dropped_fields"]
        plain = urllib.request.Request(
            f"http://127.0.0.1:{gateway.server_address[1]}/v1/chat/completions",
            data=json.dumps({"model": MODEL_NAME, "messages": [{"role": "user", "content": "hi"}]}).encode(),
            headers={"Authorization": "Bearer job-token", "Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(plain, timeout=5) as response:
            payload = json.loads(response.read())
        assert payload["model"] == "deepseek-chat-v3"
        assert gateway.calls[1]["model"] == "deepseek-chat-v3"
        assert json.loads(upstream.bodies[1]).get("stream") is not True
        before = len(upstream.bodies)
        gateway.total_tokens = MAX_TOTAL_TOKENS - 10
        blocked = urllib.request.Request(
            f"http://127.0.0.1:{gateway.server_address[1]}/v1/chat/completions",
            data=json.dumps({"model": MODEL_NAME, "messages": [{"role": "user", "content": "too close to the cap"}]}).encode(),
            headers={"Authorization": "Bearer job-token", "Content-Type": "application/json"},
            method="POST",
        )
        with pytest.raises(urllib.error.HTTPError) as caught:
            urllib.request.urlopen(blocked, timeout=5)
        assert caught.value.code == 429
        assert len(upstream.bodies) == before
        assert _estimate_tokens({"messages": [{"role": "user", "content": "hi"}]}) >= MAX_OUTPUT_TOKENS
    finally:
        _stop(gateway, gateway_thread)
        _stop(upstream, upstream_thread)
