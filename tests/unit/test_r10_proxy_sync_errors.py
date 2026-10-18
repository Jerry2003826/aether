"""Bad shared-container sync from the candidate ends the candidate session (Codex: _request/_incoming/_fill).

The candidate's reply carries the states of shared containers. Anything malformed there (not a list,
an unknown kind or handle, a kind that does not match the test's container, data that does not
decode) is a protocol violation by the candidate: the session must be marked dead and closed (the
candidate process killed), and the error is candidate_protocol, never a Python exception such as
TypeError or ValueError that a trusted test could catch as an expected outcome. Later calls on the
dead session fail without reaching the candidate.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest
from aether_worker import coding_protocol


class _Process:
    def __init__(self) -> None:
        self.killed = False

    def kill(self) -> None:
        self.killed = True

    def wait(self) -> int:
        return -9


def _session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync: object):
    session = coding_protocol._Session(tmp_path, {}, [], 5.0)
    process = _Process()
    session._process = process  # type: ignore[assignment]
    read_end, write_end = os.pipe()
    session._to, session._from = write_end, read_end
    sent: list[object] = []
    monkeypatch.setattr(coding_protocol, "_send", lambda _fd, message: sent.append(message))
    monkeypatch.setattr(coding_protocol, "_receive", lambda _fd, _deadline=None: {"ok": ["n"], "sync": sync})
    return session, process, sent


_BAD_SYNC = {
    "not_a_list": {"handle": -1},
    "entry_not_a_pair": [[-1]],
    "unknown_kind": [[-1, ["q", []]]],
    "unknown_test_handle": [[7, ["l", []]]],
    "unhashable_set_member": [[-1, ["S", [["l", []]]]]],
    "unhashable_dict_key": [[-1, ["d", [[["l", []], ["n"]]]]]],
    "bad_int": [[-1, ["l", [["i", "not-a-number"]]]]],
    "bad_float": [[-1, ["l", [["f", "not-a-float"]]]]],
    "bad_base64": [[-1, ["y", "not base64!"]]],
    "bad_nested_node": [[-1, ["l", ["plain-string"]]]],
}


@pytest.mark.parametrize("sync", list(_BAD_SYNC.values()), ids=list(_BAD_SYNC))
def test_bad_sync_from_the_candidate_kills_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, sync: object) -> None:
    session, process, sent = _session(tmp_path, monkeypatch, sync)
    with pytest.raises(coding_protocol.CandidateError) as raised:
        session.call("ping")
    assert str(raised.value) == "candidate_protocol"
    assert session._dead == "candidate_protocol"
    assert process.killed
    assert session._to is None and session._from is None
    with pytest.raises(coding_protocol.CandidateError):
        session.call("ping")
    assert len(sent) == 1


def test_a_sync_whose_kind_does_not_match_the_tests_container_kills_the_session(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    shared: list[object] = [1, 2]
    session, process, _sent = _session(tmp_path, monkeypatch, [])
    handle = session.mirror(shared)
    monkeypatch.setattr(coding_protocol, "_receive", lambda _fd, _deadline=None: {"ok": ["n"], "sync": [[handle, ["d", []]]]})
    with pytest.raises(coding_protocol.CandidateError) as raised:
        session.call("ping")
    assert str(raised.value) == "candidate_protocol"
    assert session._dead == "candidate_protocol"
    assert process.killed
    assert shared == [1, 2]


def test_fill_refuses_a_value_of_another_type() -> None:
    with pytest.raises(coding_protocol.CandidateError):
        coding_protocol._fill([], {"a": 1})


@pytest.mark.parametrize("ok", [["S", [["l", []]]], ["i", "not-a-number"], "plain-string"], ids=["unhashable", "bad_int", "not_a_node"])
def test_a_bad_result_from_the_candidate_kills_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, ok: object) -> None:
    session, process, _sent = _session(tmp_path, monkeypatch, [])
    monkeypatch.setattr(coding_protocol, "_receive", lambda _fd, _deadline=None: {"ok": ok, "sync": []})
    with pytest.raises(coding_protocol.CandidateError) as raised:
        session.call("ping")
    assert str(raised.value) == "candidate_protocol"
    assert session._dead == "candidate_protocol"
    assert process.killed


@pytest.mark.parametrize("err", [["ValueError", "x"], "boom", ["ValueError", 1, "x"]], ids=["two_items", "not_a_list", "not_text"])
def test_a_malformed_error_from_the_candidate_kills_the_session(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, err: object) -> None:
    session, process, _sent = _session(tmp_path, monkeypatch, [])
    monkeypatch.setattr(coding_protocol, "_receive", lambda _fd, _deadline=None: {"err": err, "sync": []})
    with pytest.raises(coding_protocol.CandidateError) as raised:
        session.call("ping")
    assert str(raised.value) == "candidate_protocol"
    assert session._dead == "candidate_protocol"
    assert process.killed
