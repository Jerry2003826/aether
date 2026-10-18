"""Trace redaction goes through detect-secrets, including a builtin plugin."""

from __future__ import annotations

import pytest
from detect_secrets.settings import get_plugins

from aether_worker.secret_scan import redact_trace, scanning_settings


def test_trace_tokens_and_builtin_aws_key_come_from_detect_secrets() -> None:
    sample = "sk-live-abc123 api_key=super-secret 密钥正文"
    redacted = redact_trace(sample)
    assert "sk-live-abc123" not in redacted
    assert "abc123" not in redacted
    assert "super-secret" not in redacted
    assert "密钥正文" not in redacted
    assert redacted.count("[REDACTED]") == 3
    with scanning_settings():
        plugins = get_plugins()
    aws = next(plugin for plugin in plugins if plugin.secret_type == "AWS Access Key")
    assert type(aws).__module__ == "detect_secrets.plugins.aws"
    found = redact_trace("token AKIAIOSFODNN7EXAMPLE")
    assert "AKIAIOSFODNN7EXAMPLE" not in found
    assert "[REDACTED]" in found


@pytest.mark.parametrize(
    "sample",
    [
        "connect postgresql://aether:Pg-s3cret-pw@127.0.0.1:55444/aether failed",
        "dsn host=127.0.0.1 port=55444 user=aether password=Pg-s3cret-pw dbname=aether",
        "PGPASSWORD=Pg-s3cret-pw psql -h 127.0.0.1 -p 55444",
        "connect kwargs {'host': '127.0.0.1', 'port': 55444, 'password': 'Pg-s3cret-pw'}",
        'request body {"host": "127.0.0.1", "port": 55444, "password": "Pg-s3cret-pw"}',
    ],
    ids=["url_userinfo", "dsn_password", "env_password", "python_dict", "json"],
)
def test_database_passwords_in_traces_are_redacted(sample: str) -> None:
    """A connection URL's password, a libpq-style or environment password=..., and a quoted
    'password' key are all redacted; the host and port around them stay readable."""
    redacted = redact_trace(sample)
    assert "Pg-s3cret-pw" not in redacted, redacted
    assert "s3cret" not in redacted, redacted
    assert "[REDACTED]" in redacted, redacted
    assert "127.0.0.1" in redacted, redacted
    assert "55444" in redacted, redacted
