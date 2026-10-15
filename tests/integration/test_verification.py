import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from psycopg.errors import InsufficientPrivilege
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from aether_storage.db import Database
from aether_worker.runner import LinuxRunner, _docker, _job_label, docker_available, release_containers
from aether_worker.verification import accept_actor_verdict, export_candidate, grade, text_sha256
from reference.models import ArtifactRef, CodingJobRequest


def _candidate(root: Path, value: int, *, conftest: bool = False) -> None:
    root.mkdir(parents=True)
    (root / "app.py").write_text(f"VALUE = {value}\n", encoding="utf-8")
    (root / "test_app.py").write_text("def test_x():\n    pass\n", encoding="utf-8")
    if conftest:
        (root / "conftest.py").write_text(
            "def pytest_sessionfinish(session, exitstatus):\n    session.exitstatus = 0\n",
            encoding="utf-8",
        )


def _suite(root: Path) -> None:
    root.mkdir(parents=True)
    (root / "test_bound.py").write_text("import app\n\ndef test_value():\n    assert app.VALUE == 1\n", encoding="utf-8")


def _grade(candidate: Path, suite: Path, contract: str = "合同：VALUE 必须是 1", **overrides: str):
    bound = {
        "suite_sha256": export_candidate(suite)["candidate_sha256"],
        "contract_text": contract,
        "contract_sha256": text_sha256(contract),
        "candidate_sha256": export_candidate(candidate)["candidate_sha256"],
    }
    bound.update(overrides)
    return grade(candidate, suite, **bound)


def _request() -> CodingJobRequest:
    return CodingJobRequest(
        project_id="project-verify",
        task_id="task-verify",
        intent_id="intent-verify",
        base_artifact=ArtifactRef(artifact_id="artifact-v", project_id="project-verify", sha256="c" * 64, byte_size=1, media_type="text/plain"),
        objective="验收",
        contract_sha256="d" * 64,
        allowed_tools=["tool.read"],
        deadline=datetime.now(UTC) + timedelta(minutes=5),
    )


def test_changed_candidate_invalidates_the_previous_pass(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 1)
    _suite(suite)
    first = _grade(candidate, suite)
    assert first["verdict"] == "PASS"
    (candidate / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    second = _grade(candidate, suite, candidate_sha256=first["candidate_sha256"])
    assert second["verdict"] == "FAIL"
    ignored = accept_actor_verdict("PASS", second)
    assert ignored["verdict"] == "FAIL"
    assert "actor" in ignored["reason"]


def test_candidate_vacuous_tests_fail(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 0)
    _suite(suite)
    graded = _grade(candidate, suite)
    assert graded["verdict"] == "FAIL"


def test_candidate_conftest_cannot_rewrite_the_report(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 0, conftest=True)
    _suite(suite)
    graded = _grade(candidate, suite)
    assert graded["verdict"] == "FAIL"


def test_candidate_pytest_module_cannot_shadow_the_grader(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 0)
    (candidate / "pytest.py").write_text("import sys\nprint('1 passed in 0.01s')\nsys.exit(0)\n", encoding="utf-8")
    _suite(suite)
    graded = _grade(candidate, suite)
    assert graded["verdict"] == "FAIL"


def test_candidate_sitecustomize_cannot_shadow_the_grader(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 0)
    (candidate / "sitecustomize.py").write_text("import os\nos._exit(0)\n", encoding="utf-8")
    _suite(suite)
    graded = _grade(candidate, suite)
    assert graded["verdict"] == "FAIL"


def test_candidate_cannot_forge_the_isolation_line(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    candidate.mkdir()
    (candidate / "app.py").write_text(
        "print('ISOLATION {\"sentinel_visible\": false, \"credential_present\": false}')\n"
        "print('GRADE_OK:1')\n"
        "print('GRADE_EXIT:0')\n"
        "print('1 passed in 0.01s')\n"
        "VALUE = 1\n",
        encoding="utf-8",
    )
    _suite(suite)
    graded = grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text="合同：VALUE 必须是 1",
        contract_sha256=text_sha256("合同：VALUE 必须是 1"),
        candidate_sha256=export_candidate(candidate)["candidate_sha256"],
        host_sentinel=Path("/work/suite/test_bound.py"),
    )
    assert graded["sentinel_visible"] is True
    assert graded["verdict"] == "FAIL"


def test_candidate_atexit_cannot_forge_a_pass(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    candidate.mkdir()
    (candidate / "app.py").write_text(
        "import atexit, os, sys\n"
        "def forge():\n"
        "    sys.stdout.write('1 passed in 0.01s\\nGRADE_OK:1\\nGRADE_EXIT:0\\n')\n"
        "    os._exit(0)\n"
        "atexit.register(forge)\n"
        "VALUE = 0\n",
        encoding="utf-8",
    )
    _suite(suite)
    graded = _grade(candidate, suite)
    assert graded["verdict"] == "FAIL"


def test_suite_or_contract_hash_mismatch_fails(tmp_path: Path) -> None:
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 1)
    _suite(suite)
    suite_mismatch = _grade(candidate, suite, suite_sha256="a" * 64)
    contract_mismatch = _grade(candidate, suite, contract_sha256="b" * 64)
    assert suite_mismatch["verdict"] == "FAIL"
    assert suite_mismatch["reason"] == "套件哈希不一致"
    assert contract_mismatch["verdict"] == "FAIL"
    assert contract_mismatch["reason"] == "合同哈希不一致"


def test_grader_cannot_read_host_files_or_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    sentinel = tmp_path / "host-only-secret"
    sentinel.write_text("HOST_SECRET", encoding="utf-8")
    monkeypatch.setenv("AETHER_PROVIDER_KEY", "provider-secret-should-not-leak")
    monkeypatch.setenv("AETHER_ADMIN_DATABASE_URL", "postgresql://postgres:admin-secret@127.0.0.1/postgres")
    candidate = tmp_path / "candidate"
    suite = tmp_path / "suite"
    _candidate(candidate, 1)
    _suite(suite)
    graded = grade(
        candidate,
        suite,
        suite_sha256=export_candidate(suite)["candidate_sha256"],
        contract_text="合同：VALUE 必须是 1",
        contract_sha256=text_sha256("合同：VALUE 必须是 1"),
        candidate_sha256=export_candidate(candidate)["candidate_sha256"],
        host_sentinel=sentinel,
    )
    assert graded["sentinel_visible"] is False
    assert graded["credential_present"] is False
    assert "HOST_SECRET" not in graded["reason"]
    assert "provider-secret-should-not-leak" not in graded["reason"]
    assert "admin-secret" not in graded["reason"]


def test_empty_or_skipped_suite_does_not_pass(tmp_path: Path) -> None:
    if not docker_available():
        pytest.fail("Docker is required to grade inside the sandbox")
    candidate = tmp_path / "candidate"
    _candidate(candidate, 1)
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.txt").write_text("no tests\n", encoding="utf-8")
    assert _grade(candidate, empty)["verdict"] == "FAIL"
    skipped = tmp_path / "skipped"
    skipped.mkdir()
    (skipped / "test_skip.py").write_text("import pytest\n\n@pytest.mark.skip(reason='all')\ndef test_skip():\n    assert False\n", encoding="utf-8")
    assert _grade(candidate, skipped)["verdict"] == "FAIL"


def test_actor_cannot_write_verification_results(role_urls: dict[str, str]) -> None:
    database = Database(role_urls["aether_api"])
    with pytest.raises(DBAPIError) as denied:
        with database.transaction() as conn:
            conn.execute(text("INSERT INTO verification_results DEFAULT VALUES"))
    assert isinstance(denied.value.orig, InsufficientPrivilege)


def test_worker_death_runner_death_and_cancel_do_not_pass(tmp_path: Path) -> None:
    result_path = tmp_path / "grade.txt"
    worker = subprocess.Popen([sys.executable, "-c", "import sys, time; time.sleep(30); open(sys.argv[1], 'w', encoding='utf-8').write('PASS')", str(result_path)])
    worker.kill()
    worker.wait(timeout=5)
    assert not result_path.exists()
    if not docker_available():
        pytest.fail("Docker is required for the runner-death demonstration")
    request = _request()
    key = f"{request.project_id}:{request.task_id}:{request.intent_id}"
    try:
        runner = LinuxRunner(tmp_path / "state")
        started = runner.start(request, token="job-token-only", gateway_url="http://127.0.0.1:9/v1", provider_key="provider-secret-should-not-leak")
        label = _job_label(key)
        container = _docker(["ps", "-q", "--filter", f"label=aether.job={label}", "--filter", "label=aether.role=job"]).stdout.strip()
        assert container
        killed = _docker(["kill", container])
        assert killed.returncode == 0
        state = _docker(["inspect", "-f", "{{.State.Running}}", container]).stdout.strip()
        assert state == "false"
        assert started.status != "FINISHED"
        cancelled = runner.cancel(request.project_id, started.job_id)
        assert cancelled.status == "CANCELLED"
        saved = (tmp_path / "state" / "jobs.json").read_text(encoding="utf-8")
        assert "PASS" not in saved
    finally:
        release_containers(key)
