"""A console command marked FAILED prints one diagnostic line (kind, key, exception type); nothing is retried."""

from __future__ import annotations

from contextlib import contextmanager

from aether_worker import console_commands


class _Result:
    def __init__(self, rows):
        self._rows = rows

    def mappings(self):
        return self

    def all(self):
        return self._rows


class _Nested:
    def commit(self) -> None:
        pass

    def rollback(self) -> None:
        pass


class _Conn:
    def __init__(self, rows):
        self.rows = rows
        self.updates: list[dict] = []

    def execute(self, statement, params=None):
        sql = str(statement)
        if sql.startswith("SELECT id::text"):
            return _Result(self.rows)
        self.updates.append(dict(params or {}))
        return _Result([])

    def begin_nested(self):
        return _Nested()


class _Database:
    def __init__(self, conn):
        self.conn = conn

    @contextmanager
    def transaction(self, **_kwargs):
        yield self.conn


def test_a_failed_console_command_prints_a_diagnostic_line(monkeypatch, capsys) -> None:
    def _boom(*_args, **_kwargs):
        raise ValueError("source_kind_invalid")

    monkeypatch.setattr(console_commands, "_apply", _boom)
    conn = _Conn([{"id": "c1", "command_key": "k-fetch", "kind": "source.create", "payload": {}}])
    applied = console_commands.apply_console_commands(_Database(conn), owner_id="o", project_id="p")
    assert applied == ["k-fetch"]
    assert conn.updates[-1]["status"] == "FAILED"
    out = capsys.readouterr().out
    assert "aether-console-command-failed kind=source.create command_key=k-fetch error=ValueError\n" in out
