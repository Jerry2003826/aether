"""Trace redaction goes through detect-secrets, including a builtin plugin."""

from __future__ import annotations

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
