"""detect-secrets keeps its settings and plugin caches process-wide. Worker activities redact
traces on several threads at once, so one scan's settings context must never be undone while another
scan is still running: every scan finds what the trace detector finds, whatever the other threads do."""

from __future__ import annotations

import threading

import pytest
from aether_worker import secret_scan

# Readable fixture phrase that only the Aether trace detector (密钥\S*) reports.
_TRACE_SECRET = "密钥是固定的测试短语"


def test_a_scan_keeps_its_settings_while_another_scan_ends(monkeypatch: pytest.MonkeyPatch) -> None:
    real_scan_line = secret_scan.scan_line
    first_inside = threading.Event()
    second_inside = threading.Event()
    first_done = threading.Event()

    def scan_line(text: str):
        if threading.current_thread().name == "first-scan":
            first_inside.set()
            # Unsynchronised, the second scan enters now; serialised, it cannot until this one ends.
            second_inside.wait(timeout=1.0)
        else:
            second_inside.set()
            # Unsynchronised, the first scan restores the earlier settings while this one waits.
            first_done.wait(timeout=1.0)
        return real_scan_line(text)

    monkeypatch.setattr(secret_scan, "scan_line", scan_line)
    found: dict[str, list[str]] = {}

    def run(name: str) -> None:
        found[name] = [secret.secret_value or "" for secret in secret_scan.scan_text(f"value {_TRACE_SECRET}")]
        if name == "first-scan":
            first_done.set()

    first = threading.Thread(target=run, args=("first-scan",), name="first-scan")
    first.start()
    assert first_inside.wait(timeout=5.0)
    second = threading.Thread(target=run, args=("second-scan",), name="second-scan")
    second.start()
    first.join(timeout=10.0)
    second.join(timeout=10.0)
    assert any(_TRACE_SECRET in value for value in found["first-scan"])
    assert any(_TRACE_SECRET in value for value in found["second-scan"])


def test_concurrent_redactions_all_mask_the_trace_secret() -> None:
    results: list[str] = []
    lock = threading.Lock()
    start = threading.Barrier(8)

    def run() -> None:
        start.wait(timeout=5.0)
        for _ in range(25):
            redacted = secret_scan.redact_trace(f"call failed {_TRACE_SECRET} done")
            with lock:
                results.append(redacted)

    threads = [threading.Thread(target=run) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=60.0)
    assert len(results) == 200
    assert all(_TRACE_SECRET not in text for text in results)
