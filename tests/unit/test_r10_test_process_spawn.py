"""Threat model I-16: a trusted test must not start processes. A child of the test process would run
candidate code (or anything) outside the candidate's Landlock sandbox, with the test process's view of
the grading workspace. Every way to start one is refused and fails the test even if the test catches
the refusal; the protocol's own candidate process still starts. The report pipe is not inheritable."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PROTOCOL = Path("services/worker/aether_worker/coding_protocol.py").resolve()
_NONCE = "test process spawn nonce"
_APP = "def value():\n    return 42\n\nif __name__ == '__main__':\n    print(value())\n"


def _grade(tmp_path: Path, test: str) -> tuple[int, dict, str]:
    files = {"app.py": _APP, "test_app.py": test}
    base, job = tmp_path / "baseline", tmp_path / "job"
    for root in (base, job):
        root.mkdir()
        for name, text in files.items():
            (root / name).write_text(text, encoding="utf-8")
    secret = tmp_path / "nonce"
    secret.write_text(_NONCE, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_")}
    env.update({"AETHER_PROTOCOL_NONCE_FILE": str(secret), "AETHER_PROTOCOL_SHM_DIR": "/dev/shm", "AETHER_PROTOCOL_BASELINE_DIR": str(base)})
    completed = subprocess.run([sys.executable, "-I", str(_PROTOCOL)], cwd=job, env=env, capture_output=True, text=True, timeout=120, check=False)
    signed = [body for body in map(_json, completed.stdout.splitlines()) if isinstance(body, dict) and body.get("nonce") == _NONCE]
    output = completed.stdout + completed.stderr
    assert len(signed) == 1, output[-3000:]
    return completed.returncode, signed[0], output


def _json(line: str) -> object:
    try:
        return json.loads(line)
    except ValueError:
        return None


_REFUSED = "candidate_object_unsupported:process_spawn"

_SPAWNS = {
    "subprocess_run": "import subprocess, sys\n    out = subprocess.run([sys.executable, 'app.py'], capture_output=True, text=True).stdout\n    assert out.strip() == '42'\n",
    "subprocess_caught": "import subprocess, sys\n    try:\n        subprocess.run([sys.executable, '-c', 'pass'])\n    except BaseException:\n        pass\n",
    "os_system": "import os\n    try:\n        os.system('true')\n    except BaseException:\n        pass\n",
    "os_fork": "import os\n    try:\n        pid = os.fork()\n    except BaseException:\n        return\n    if pid == 0:\n        os._exit(0)\n    os.waitpid(pid, 0)\n",
    "os_posix_spawn": "import os, sys\n    try:\n        pid = os.posix_spawn(sys.executable, [sys.executable, '-c', 'pass'], {})\n        os.waitpid(pid, 0)\n    except BaseException:\n        pass\n",
    "os_execv": "import os, sys\n    try:\n        os.execv(sys.executable, [sys.executable, '-c', 'pass'])\n    except BaseException:\n        pass\n",
    "multiprocessing_spawn": "import multiprocessing\n    try:\n        process = multiprocessing.get_context('spawn').Process(target=print)\n        process.start()\n        process.join()\n    except BaseException:\n        pass\n",
    "multiprocessing_fork": "import multiprocessing\n    try:\n        process = multiprocessing.get_context('fork').Process(target=print)\n        process.start()\n        process.join()\n    except BaseException:\n        pass\n",
    "ctypes_fork": "import ctypes\n    try:\n        ctypes.CDLL(None).fork\n    except BaseException:\n        pass\n",
}


@pytest.mark.parametrize("body", list(_SPAWNS.values()), ids=list(_SPAWNS))
def test_a_test_that_starts_a_process_fails(tmp_path: Path, body: str) -> None:
    code, result, output = _grade(tmp_path, "def test_value():\n    " + body)
    assert code == 1, (result, output[-2000:])
    assert result["cases"] == [{"name": "test_app.test_value", "verdict": "FAIL", "detail": _REFUSED}], result


def test_the_protocols_own_candidate_process_still_starts(tmp_path: Path) -> None:
    code, result, output = _grade(tmp_path, "import app\n\ndef test_value():\n    assert app.value() == 42\n")
    assert result["cases"] == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}], (result, output[-2000:])
    assert code == 0


def test_the_report_pipe_is_not_inheritable(tmp_path: Path) -> None:
    test = (
        "import os\n\n"
        "def test_value():\n"
        "    pipes = []\n"
        "    for fd in map(int, os.listdir('/proc/self/fd')):\n"
        "        try:\n"
        "            target = os.readlink(f'/proc/self/fd/{fd}')\n"
        "        except OSError:\n"
        "            continue  # the directory listdir itself opened\n"
        "        if fd > 2 and target.startswith('pipe:'):\n"
        "            pipes.append(fd)\n"
        "    assert pipes\n"
        "    assert [fd for fd in pipes if os.get_inheritable(fd)] == []\n"
    )
    code, result, output = _grade(tmp_path, test)
    assert result["cases"] == [{"name": "test_app.test_value", "verdict": "PASS", "detail": ""}], (result, output[-2000:])
    assert code == 0
