"""Codex P1 (coding_protocol.py:902): /dev/shm must not carry grading files from the agent to the candidate.

The agent and the candidate share the sandbox's /dev/shm, and the candidate may use it (POSIX semaphores
and shared memory live there). An agent that copies the trusted tests and the oracle into it before
finishing must not leave them for the candidate to read at grading: the entry empties it once every
agent process is gone, before any candidate code runs. Real entry, real protocol, scripted agent.

Each test uses its own directory under /dev/shm as the sandbox's shared memory (AETHER_SANDBOX_SHM),
so nothing else in this machine's /dev/shm is touched.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Iterator
from pathlib import Path

import pytest

from tests.unit.test_openhands_coding import _ENTRY, _PYTHON, _SocketModel

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_SECRET = "s3cr3t-expected-4f1d"


@pytest.fixture
def shm() -> Iterator[Path]:
    path = Path(tempfile.mkdtemp(prefix="aether-unit-shm-", dir="/dev/shm"))
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _leaking_app(shm: Path) -> str:
    return (
        "import os\n"
        f"SHM = {str(shm)!r}\n"
        "\n"
        "def value():\n"
        "    return 42\n"
        "\n"
        "def leak():\n"
        "    found = {}\n"
        "    for name in ('test_app.py', 'oracle.py'):\n"
        "        try:\n"
        "            with open(os.path.join(SHM, name), encoding='utf-8') as handle:\n"
        "                found[name] = handle.read()\n"
        "        except OSError:\n"
        "            pass\n"
        "    try:\n"
        "        listing = sorted(os.listdir(SHM))\n"
        "    except OSError:\n"
        "        listing = None\n"
        "    return found, listing\n"
    )


_TESTS = (
    "import app\n"
    "from oracle import EXPECTED\n"
    "\n"
    "def test_value():\n"
    "    assert app.value() == 42 and EXPECTED\n"
    "\n"
    "def test_shared_memory_holds_nothing_from_the_agent():\n"
    "    found, listing = app.leak()\n"
    "    assert found == {} and not listing, (sorted(found), listing)\n"
)


def _run(tmp_path: Path, command: str, files: dict[str, str], **extra: str) -> tuple[subprocess.CompletedProcess[str], dict]:
    assert _PYTHON.is_file(), "environments/openhands lock was not synced"
    work = tmp_path / "work"
    job = tmp_path / "job"
    work.mkdir()
    for name, text in files.items():
        (work / name).write_text(text, encoding="utf-8")
    fake = _SocketModel(terminal_command=command)
    fake.start()
    env = {key: value for key, value in os.environ.items() if key not in {"AETHER_SANDBOX_SHM", "AETHER_LIVE_MODEL_KEY"}}
    env.update(
        {
            "OPENHANDS_SUPPRESS_BANNER": "1",
            "AETHER_SANDBOX_WORK": str(work),
            "AETHER_SANDBOX_JOB": str(job),
            "AETHER_SANDBOX_PROTOCOL": str(_PROTOCOL),
            "AETHER_MODEL_SOCKET": str(fake.path),
            "AETHER_JOB_TOKEN": "job-token-unit",
            "AETHER_MODEL_ALIAS": "openai/coder",
            "PYTHONDONTWRITEBYTECODE": "1",
            "AETHER_OBJECTIVE": "Make value() return 42.",
            **extra,
        }
    )
    try:
        completed = subprocess.run(
            [str(_PYTHON.absolute()), "-I", str(_ENTRY.resolve())],
            cwd=tmp_path, env=env, capture_output=True, text=True, timeout=180, check=False,
        )
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout[-4000:] + completed.stderr[-4000:]
    return completed, json.loads(line[len("RESULT ") :])


def test_grading_files_the_agent_copies_into_shared_memory_are_gone_before_the_candidate_runs(tmp_path: Path, shm: Path) -> None:
    job = tmp_path / "job"
    ran = tmp_path / "copied"
    # The agent's last act: the trusted tests and the oracle into shared memory, plus a nested copy.
    command = (
        f"cp {job}/test_app.py {job}/oracle.py {shm}/ && mkdir -p {shm}/deep/er && "
        f"cp {job}/oracle.py {shm}/deep/er/ && chmod 000 {shm}/deep && touch {ran}"
    )
    files = {"app.py": _leaking_app(shm), "test_app.py": _TESTS, "oracle.py": f"EXPECTED = {_SECRET!r}\n"}
    completed, body = _run(tmp_path, command, files, AETHER_SANDBOX_SHM=str(shm))
    assert ran.is_file(), completed.stdout[-4000:] + completed.stderr[-4000:]
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    assert verdicts == {
        "test_app.test_value": "PASS",
        "test_app.test_shared_memory_holds_nothing_from_the_agent": "PASS",
    }, body
    assert completed.returncode == 0, body
    # Emptied, not just hidden: nothing the agent put there is left.
    assert list(shm.iterdir()) == [], sorted(path.name for path in shm.iterdir())


def test_without_a_configured_shared_memory_directory_nothing_is_graded(tmp_path: Path) -> None:
    marker = tmp_path / "candidate-ran"
    files = {
        "app.py": f"open({str(marker)!r}, 'w').write('ran')\n\ndef value():\n    return 42\n",
        "test_app.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
    }
    completed, body = _run(tmp_path, "true", files)
    assert completed.returncode == 2, body
    assert body["cases"] == [{"name": "runner", "verdict": "FAIL", "detail": "shared_memory_unconfigured"}], body
    assert not marker.exists()


_MP_APP = (
    "import multiprocessing\n"
    "\n"
    "def _square(x):\n"
    "    return x * x\n"
    "\n"
    "def value():\n"
    "    lock = multiprocessing.Lock()\n"
    "    with lock:\n"
    "        pass\n"
    "    semaphore = multiprocessing.Semaphore(2)\n"
    "    semaphore.acquire()\n"
    "    semaphore.release()\n"
    "    queue = multiprocessing.Queue()\n"
    "    queue.put(3)\n"
    "    got = queue.get(timeout=10)\n"
    "    with multiprocessing.get_context('fork').Pool(2) as pool:\n"
    "        squares = pool.map(_square, [1, 2, 3])\n"
    "    return [got, squares]\n"
)


def test_candidate_multiprocessing_still_works_under_the_protocol(tmp_path: Path) -> None:
    """Semaphores, queues and pools need shared memory; the candidate keeps them."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "app.py").write_text(_MP_APP, encoding="utf-8")
    (work / "test_app.py").write_text("import app\n\ndef test_mp():\n    assert app.value() == [3, [1, 4, 9]]\n", encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    completed = subprocess.run(
        [sys.executable, "-I", str(_PROTOCOL)], cwd=work, env=env, capture_output=True, text=True, timeout=120, check=False
    )
    body = json.loads(completed.stdout.strip().splitlines()[-1])
    assert body["cases"] == [{"name": "test_app.test_mp", "verdict": "PASS", "detail": ""}], (body, completed.stderr[-2000:])
    assert completed.returncode == 0


@pytest.mark.parametrize("configured", [None, "", "relative/shm", "/nonexistent/aether-shm"], ids=["unset", "empty", "relative", "missing"])
def test_the_protocol_grades_nothing_without_its_shared_memory_directory(tmp_path: Path, configured: str | None) -> None:
    """No AETHER_PROTOCOL_SHM_DIR (or not an existing absolute directory): no fallback to /dev/shm,
    which nobody emptied; nothing is graded and no candidate code runs."""
    work = tmp_path / "work"
    work.mkdir()
    marker = Path(tempfile.mkdtemp(prefix="aether-unit-marker-", dir="/dev/shm"))
    try:
        (work / "app.py").write_text(
            f"open({str(marker / 'ran')!r}, 'w').write('ran')\n\ndef value():\n    return 42\n", encoding="utf-8"
        )
        (work / "test_app.py").write_text("import app\n\ndef test_value():\n    assert app.value() == 42\n", encoding="utf-8")
        env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
        if configured is not None:
            env["AETHER_PROTOCOL_SHM_DIR"] = configured
        completed = subprocess.run(
            [sys.executable, "-I", str(_PROTOCOL)], cwd=work, env=env, capture_output=True, text=True, timeout=120, check=False
        )
        body = json.loads(completed.stdout.strip().splitlines()[-1])
        assert body["cases"] == [{"name": "suite", "verdict": "FAIL", "detail": "shared_memory_unconfigured"}], body
        assert body["passed"] == 0 and body["failed"] == 1, body
        assert completed.returncode == 1
        assert not (marker / "ran").exists()
    finally:
        shutil.rmtree(marker, ignore_errors=True)
