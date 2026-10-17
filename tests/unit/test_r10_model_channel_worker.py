"""The worker closes the model channel itself and accepts grading only after the sandbox closed it first."""

from __future__ import annotations

import socket
import threading
from pathlib import Path
from typing import Any

import pytest

from aether_worker import coding_jobs
from aether_worker.coding_jobs import _coding_terminal
from aether_worker.coding_model_proxy import CodingModelProxy
from aether_worker.coding_sandbox import CodingSandboxError
from test_p1_proxy_drain import _KEY, _PROJECT, _TASK, _Harness, _post

_GRADED = {
    "passed": 1,
    "failed": 0,
    "agent_completed": True,
    "model_requests": 1,
    "patch": "--- a/app.py\n+++ b/app.py\n",
    "cases": [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}],
}


def _revoke_in_band(path: Path, token: str) -> int:
    request = (
        f"POST /aether/revoke HTTP/1.1\r\nauthorization: Bearer {token}\r\ncontent-length: 0\r\nconnection: close\r\n\r\n"
    ).encode()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(str(path))
        sock.sendall(request)
        chunks = []
        while block := sock.recv(4096):
            chunks.append(block)
    return int(b"".join(chunks).split(b" ", 2)[1])


class _Job(_Harness):
    """_Harness whose sandbox returns a graded result, revoking in band only when told to."""

    def __init__(
        self,
        monkeypatch: pytest.MonkeyPatch,
        *,
        revoke: bool,
        in_flight: bool = False,
        fails: BaseException | None = None,
        first_call: tuple[int, int] | None = None,
        release: bool = True,
    ) -> None:
        super().__init__(
            monkeypatch,
            first_call=first_call,
            release_after=0.5 if in_flight and release else None,
            fails=fails,
        )
        self.revoke = revoke
        self.in_flight = in_flight
        self.published: list[tuple[str, str | None]] = []
        self.at_drain: list[tuple[bool, bool]] = []
        harness = self
        monkeypatch.setattr(
            coding_jobs,
            "_publish",
            lambda *_a, **k: (
                harness.published.append((k["state"], k["error_code"])) or True
            ),
        )
        real_drain = CodingModelProxy.drain

        def drain(proxy: CodingModelProxy, **kwargs: Any) -> int:
            harness.at_drain.append((proxy.revoked, proxy.socket_path.exists()))
            return real_drain(proxy, **kwargs)

        monkeypatch.setattr(CodingModelProxy, "drain", drain)

    def sandbox(
        self, _root: Path, *, proxy_dir: Path, job_token: str, **_kwargs: Any
    ) -> dict[str, Any]:
        socket_path = proxy_dir / "model.sock"
        if self.first_call is not None:
            self._open_call()
            self._answer(*self.first_call)
        if self.in_flight:
            threading.Thread(target=_post, args=(socket_path, job_token), daemon=True).start()
            assert self.entered.wait(10), "the in-flight call never reached the proxy"
            if self.release_after is not None:
                timer = threading.Timer(self.release_after, self.release.set)
                self.timers.append(timer)
                timer.start()
        if self.revoke:
            assert _revoke_in_band(socket_path, job_token) == 200
        if self.fails is not None:
            raise self.fails
        return {"exit_code": 0, "result": dict(_GRADED)}


def test_the_worker_revokes_the_channel_itself_when_the_sandbox_never_did(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _Job(monkeypatch, revoke=False)
    job.run()
    assert job.at_drain and job.at_drain[0] == (True, False), job.at_drain
    # Grading the sandbox reported without first closing the channel is not accepted.
    assert job.published == [("FAILED", "model_channel_open")]


def test_the_worker_revokes_the_channel_when_the_sandbox_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _Job(monkeypatch, revoke=False, fails=CodingSandboxError("sandbox_failed"))
    job.run()
    assert job.at_drain and job.at_drain[0] == (True, False), job.at_drain


def test_grading_after_an_in_band_revoke_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    job = _Job(monkeypatch, revoke=True)
    job.run()
    assert job.published == [("FINISHED", None)]


def test_a_call_in_flight_at_the_in_band_revoke_is_still_billed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "20")
    job = _Job(monkeypatch, revoke=True, in_flight=True)
    job.run()
    assert job.done_at_settle == [True]
    row = job.db.request(_PROJECT, _TASK, _KEY)
    assert row["usage_status"] == "known"
    assert (row["input_tokens"], row["output_tokens"]) == (120, 30)
    assert "unknown_usage_calls" not in row["usage_detail"]
    assert "proxy_drained" not in row["usage_detail"]
    assert job.published == [("FINISHED", None)]


def test_the_entry_failure_code_is_its_own_terminal_error() -> None:
    sandbox = {
        "exit_code": 1,
        "result": {
            "passed": 0,
            "failed": 1,
            "agent_completed": True,
            "model_requests": 1,
            "patch": "--- a/app.py\n+++ b/app.py\n",
            "cases": [
                {"name": "runner", "verdict": "FAIL", "detail": "model_channel_open"}
            ],
        },
    }
    assert _coding_terminal(
        sandbox, upstream_error=None, measured=(3, 4), usage_missing=False
    ) == ("FAILED", "model_channel_open")
    assert "model_channel_open" not in coding_jobs.GRADING_VIOLATIONS


def test_a_graded_result_needs_the_channel_closed_before_the_container_ended() -> None:
    sandbox = {"exit_code": 0, "result": dict(_GRADED)}
    assert _coding_terminal(
        sandbox,
        upstream_error=None,
        measured=(3, 4),
        usage_missing=False,
        channel_revoked=False,
    ) == (
        "FAILED",
        "model_channel_open",
    )
    assert _coding_terminal(
        sandbox,
        upstream_error=None,
        measured=(3, 4),
        usage_missing=False,
        channel_revoked=True,
    ) == ("FINISHED", None)


def test_a_call_crossing_the_revoke_that_outlasts_the_drain_is_charged_its_reservation(monkeypatch: pytest.MonkeyPatch) -> None:
    from test_p1_proxy_drain import _CALL

    monkeypatch.setenv("AETHER_CODING_PROXY_DRAIN_SECONDS", "0.3")
    job = _Job(monkeypatch, revoke=True, in_flight=True, first_call=(100, 50), release=False)
    job.run()
    assert job.done_at_settle == [False]
    row = job.db.request(_PROJECT, _TASK, _KEY)
    assert row["usage_status"] == "known"
    assert (row["input_tokens"], row["output_tokens"]) == (100, 50)
    assert row["usage_detail"]["proxy_drained"] is True
    assert row["usage_detail"]["unknown_usage_calls"] == 1
    assert row["usage_detail"]["unknown_reserved_tokens"] == _CALL
    account = job.db.account(_PROJECT, _TASK)
    assert account["spent_micro_usd"] == 150 + _CALL
    assert account["held_micro_usd"] == 0
