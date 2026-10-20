"""Shared builtin containers are re-encoded on every request (Codex: _outgoing), so their total size
is bounded: past the cap the session fails closed with shared_containers_too_large, before anything
is sent to the candidate, instead of every call re-dumping an unbounded state.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from aether_worker import coding_protocol


class _Process:
    killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return -9


def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    session = coding_protocol._Session(tmp_path, {}, [], 5.0)
    process = _Process()
    session._process = process  # type: ignore[assignment]
    read_end, write_end = os.pipe()
    session._to, session._from = write_end, read_end
    sent: list[object] = []
    monkeypatch.setattr(coding_protocol, "_send", lambda _fd, message: sent.append(message))
    monkeypatch.setattr(coding_protocol, "_receive", lambda _fd, _deadline=None: {"ok": ["n"], "sync": []})
    return session, process, sent


_KIB_TEXT = "shared container filler " * 43  # about 1 KiB of readable text


def test_shared_containers_past_the_total_cap_fail_closed(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, process, sent = _session(tmp_path, monkeypatch)
    session.mirror([_KIB_TEXT] * (9 * 1024))  # about 9 MiB once encoded: over the 8 MiB cap
    with pytest.raises(coding_protocol.CandidateError) as raised:
        session.call("ping")
    assert str(raised.value) == "shared_containers_too_large"
    assert session._dead == "shared_containers_too_large"
    assert process.killed
    assert sent == []


def test_the_cap_counts_every_shared_container_not_only_changed_ones(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, process, sent = _session(tmp_path, monkeypatch)
    session.mirror([_KIB_TEXT] * (5 * 1024))
    session.call("ping")
    assert len(sent) == 1
    # Unchanged, the first container is still re-encoded on every request, so it still counts.
    session.mirror([_KIB_TEXT] * (4 * 1024))
    with pytest.raises(coding_protocol.CandidateError) as raised:
        session.call("ping")
    assert str(raised.value) == "shared_containers_too_large"
    assert process.killed
    assert len(sent) == 1


def test_shared_containers_under_the_cap_still_cross(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    session, _process, sent = _session(tmp_path, monkeypatch)
    session.mirror([_KIB_TEXT] * 1024)
    session.call("ping")
    session.call("ping")
    assert len(sent) == 2
    assert len(sent[0]["sync"]) == 1  # type: ignore[index]
    assert sent[1]["sync"] == []  # type: ignore[index]
