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
from aether_worker.verification import accept_actor_verdict, export_candidate, grade, prepare_workspace
from reference.models import ArtifactRef, CodingJobRequest


def _tree(root: Path, passing: bool) -> None:
    root.mkdir(parents=True)
    (root / "app.py").write_text("VALUE = 1\n", encoding="utf-8")
    body = "def test_value():\n    assert True\n" if passing else "def test_value():\n    assert False\n"
    (root / "test_app.py").write_text(body, encoding="utf-8")


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
    source = tmp_path / "candidate"
    _tree(source, passing=True)
    exported = export_candidate(source)
    workspace = tmp_path / "workspace"
    prepare_workspace(source, workspace)
    suite = "a" * 64
    contract = "b" * 64
    first = grade(workspace, suite_sha256=suite, contract_sha256=contract, candidate_sha256=exported["candidate_sha256"])
    assert first["verdict"] == "PASS"
    (source / "app.py").write_text("VALUE = 2\n", encoding="utf-8")
    changed = export_candidate(source)
    assert changed["candidate_sha256"] != exported["candidate_sha256"]
    prepare_workspace(source, workspace)
    second = grade(workspace, suite_sha256=suite, contract_sha256=contract, candidate_sha256=exported["candidate_sha256"])
    assert second["verdict"] == "FAIL"
    ignored = accept_actor_verdict("PASS", second)
    assert ignored["verdict"] == "FAIL"
    assert "actor" in ignored["reason"]


def test_empty_skipped_and_forged_junit_do_not_pass(tmp_path: Path) -> None:
    empty = tmp_path / "empty"
    empty.mkdir()
    (empty / "readme.txt").write_text("no tests\n", encoding="utf-8")
    empty_export = export_candidate(empty)
    empty_grade = grade(empty, suite_sha256="a" * 64, contract_sha256="b" * 64, candidate_sha256=empty_export["candidate_sha256"])
    assert empty_grade["verdict"] == "FAIL"
    skipped = tmp_path / "skipped"
    skipped.mkdir()
    (skipped / "test_skip.py").write_text("import pytest\n\n@pytest.mark.skip(reason='all')\ndef test_skip():\n    assert False\n", encoding="utf-8")
    skipped_export = export_candidate(skipped)
    skipped_grade = grade(skipped, suite_sha256="a" * 64, contract_sha256="b" * 64, candidate_sha256=skipped_export["candidate_sha256"])
    assert skipped_grade["verdict"] == "FAIL"
    forged = tmp_path / "forged"
    _tree(forged, passing=False)
    (forged / "junit.xml").write_text('<testsuite tests="1" failures="0" errors="0"></testsuite>\n', encoding="utf-8")
    forged_export = export_candidate(forged)
    forged_grade = grade(forged, suite_sha256="a" * 64, contract_sha256="b" * 64, candidate_sha256=forged_export["candidate_sha256"])
    assert forged_grade["verdict"] == "FAIL"


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
