"""A console command that fails is marked FAILED for good; the worker must say so (r11c, triage
category D: sys01_followup local_document_indexes polled 90 s for a source that never appeared, and the
worker log had no line for the command that produced it)."""

from __future__ import annotations

import contextlib

from aether_worker import console_commands


class _Result:
    def __init__(self, rows):
        self.rows = rows

    def mappings(self):
        return self

    def all(self):
        return self.rows


class _Nested:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _Conn:
    def __init__(self) -> None:
        self.updates: list[dict] = []

    def execute(self, statement, params=None):
        if "SELECT" in str(statement):
            return _Result([{"id": "c1", "command_key": "k1", "kind": "source.create", "payload": {"source_id": "s1"}}])
        self.updates.append(dict(params or {}))
        return _Result([])

    def begin_nested(self):
        return _Nested()


class _Database:
    def __init__(self) -> None:
        self.conn = _Conn()

    @contextlib.contextmanager
    def transaction(self, **kwargs):
        yield self.conn


def test_a_failed_console_command_is_logged_with_its_kind_and_error(monkeypatch, capsys) -> None:
    def broken(*args, **kwargs):
        raise ValueError("bad payload")

    monkeypatch.setattr(console_commands, "_apply", broken)
    database = _Database()
    console_commands.apply_console_commands(database, owner_id="o", project_id="p")  # type: ignore[arg-type]
    assert [update["status"] for update in database.conn.updates] == ["FAILED"]
    out = capsys.readouterr().out.splitlines()
    assert "aether-console-command-failed project=p kind=source.create command=k1 error=ValueError" in out, out
