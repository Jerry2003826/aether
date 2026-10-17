"""A foreground `docker run` keeps only a bounded tail of each stream, never the whole output.

The grader runs candidate code, so its container can print without limit. No real container runs:
the fake SDK container hands out a lazy chunk stream (the same chunk object again and again), so
the only large memory the run can hold is what container_runtime itself keeps.
"""

from __future__ import annotations

import subprocess
import tracemalloc
from collections.abc import Iterator

from aether_worker import coding_sandbox
from aether_worker.container_runtime import execute_docker, set_docker_client

_CHUNK = 65536
_STDOUT_CAP = coding_sandbox.RESULT_CAPTURE_LIMIT
_STDERR_CAP = coding_sandbox.LOG_CAPTURE_LIMIT
_OUTPUT_BYTES = 8 * _STDOUT_CAP


def _chunks(fill: bytes, last: bytes) -> Iterator[bytes]:
    block = fill * _CHUNK
    for _ in range(_OUTPUT_BYTES // _CHUNK):
        yield block
    yield last


class _Container:
    id = "cid-loud"

    def wait(self, **_kwargs: object) -> dict[str, int]:
        return {"StatusCode": 0}

    def logs(self, *, stdout: bool, stderr: bool, stream: bool = False, follow: bool = False) -> object:
        assert stdout != stderr
        chunks = _chunks(b"o", b"STDOUT-END\n") if stdout else _chunks(b"e", b"STDERR-END\n")
        if stream:
            return chunks
        # Without stream=True docker-py reads the whole body into one bytes object.
        return b"".join(chunks)

    def remove(self, **_kwargs: object) -> None:
        pass


class _Containers:
    def run(self, image: str, command: list[str] | None, **_kwargs: object) -> _Container:
        return _Container()


class _Images:
    def get(self, reference: str) -> object:
        return object()


class _Client:
    containers = _Containers()
    images = _Images()


def _run_loud() -> tuple[subprocess.CompletedProcess[str], int]:
    set_docker_client(_Client())
    tracemalloc.start()
    try:
        completed = execute_docker(["run", "--rm", "aether-grader:local", "python", "-c", "spam"], timeout=30)
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
        set_docker_client(None)
    return completed, peak


def test_a_loud_foreground_run_keeps_only_the_bounded_tails() -> None:
    completed, peak = _run_loud()
    # Memory held during the run is a small multiple of the caps (raw tail, joined bytes, decoded
    # text), never proportional to the 8x-cap output the container wrote.
    assert peak < 3 * (_STDOUT_CAP + _STDERR_CAP), f"peak {peak} bytes"
    assert peak < _OUTPUT_BYTES // 2
    assert completed.returncode == 0
    assert len(completed.stdout.encode()) <= _STDOUT_CAP
    assert len(completed.stderr.encode()) <= _STDERR_CAP
    # The tail is what survives: the last bytes each stream wrote.
    assert completed.stdout.endswith("STDOUT-END\n")
    assert completed.stderr.endswith("STDERR-END\n")
