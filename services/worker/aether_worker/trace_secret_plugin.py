"""detect-secrets plugin for trace redaction. The denylist is the former handwritten set."""

from __future__ import annotations

import re

from detect_secrets.plugins.base import RegexBasedDetector


class TraceSecretDetector(RegexBasedDetector):
    """Whole-token patterns previously applied by redact_trace."""

    secret_type = "Aether Trace Secret"

    denylist = (
        re.compile(r"sk-live-[A-Za-z0-9_-]+"),
        re.compile(r"api_key=\S+"),
        # Unquoted password=... (libpq DSN, PGPASSWORD=...), which KeywordDetector only finds quoted.
        re.compile(r"(?i)password=\S+"),
        re.compile(r"密钥\S*"),
    )
