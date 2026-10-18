"""In the real coding sandbox, the agent cannot hand grading files to the candidate through /dev/shm.

The real entry runs in the sandbox image with the job's container flags (coding_sandbox.container_arguments:
read-only rootfs, no network, cap-drop ALL, uid 65534, the workspace on /work read-only, the model socket on
/run/aether/proxy). A scripted model has the agent copy the trusted tests and the oracle into /dev/shm
(the container's own) and finish; at grading the candidate must find nothing there, and its
multiprocessing (semaphores, queues and pools need /dev/shm) still works.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from aether_worker.coding_sandbox import container_arguments
from aether_worker.container_runtime import execute_docker
from aether_worker.openhands_image import ensure_openhands_sandbox
from aether_worker.runner import docker_available

from tests.unit.test_openhands_coding import _SocketModel
from tests.unit.test_r10_shm_isolation import _MP_APP, _SECRET, _leaking_app

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
    "\n"
    "def test_multiprocessing_still_works():\n"
    "    assert app.mp_value() == [3, [1, 4, 9]]\n"
)


def test_agent_copies_in_dev_shm_never_reach_the_candidate(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not docker_available():
        pytest.fail("Docker is required for the coding sandbox check")
    image = ensure_openhands_sandbox()
    workspace = tmp_path / "work"
    proxy = tmp_path / "proxy"
    workspace.mkdir()
    proxy.mkdir()
    app = _leaking_app(Path("/dev/shm")) + "\n" + _MP_APP.replace("def value():", "def mp_value():")
    for name, text in {"app.py": app, "test_app.py": _TESTS, "oracle.py": f"EXPECTED = {_SECRET!r}\n"}.items():
        (workspace / name).write_text(text, encoding="utf-8")
    for path in [workspace, *workspace.rglob("*")]:
        path.chmod(0o755 if path.is_dir() else 0o644)
    proxy.chmod(0o777)
    monkeypatch.setenv("TMPDIR", str(proxy))
    # The agent's last act, from its job directory: the tests and the oracle into the shared memory.
    fake = _SocketModel(terminal_command="cp test_app.py oracle.py /dev/shm/ && echo ran > ran.txt")
    socket_name = fake.path.name
    fake.start()
    fake.path.chmod(0o777)
    try:
        args = container_arguments(workspace, image, objective="Make value() return 42.", proxy_dir=proxy, job_token="job-token-unit")
        assert args[-4:] == [image, "/opt/openhands/.venv/bin/python", "-I", "/opt/aether/openhands_sandbox_entry.py"]
        # Same flags; only the socket's file name is the fake's.
        command = ["run", "--rm", *args[1:-4], "-e", f"AETHER_MODEL_SOCKET=/run/aether/proxy/{socket_name}", *args[-4:]]
        completed = execute_docker(command, timeout=300)
    finally:
        fake.close()
    line = next((item for item in reversed(completed.stdout.splitlines()) if item.startswith("RESULT ")), "")
    assert line, completed.stdout[-4000:] + completed.stderr[-4000:]
    body = json.loads(line[len("RESULT ") :])
    assert "+++ b/ran.txt" in body.get("patch", ""), body  # the agent's command ran in its job directory
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    assert verdicts == {
        "test_app.test_value": "PASS",
        "test_app.test_shared_memory_holds_nothing_from_the_agent": "PASS",
        "test_app.test_multiprocessing_still_works": "PASS",
    }, body
    assert completed.returncode == 0, body
