import os
from pathlib import Path

import pytest

from aether_worker.runner import docker_available
from aether_worker.skills import SkillError, _script_bytes, execute_script, propose_candidate, scripts_tree_sha256


def _require_docker() -> None:
    if not docker_available():
        pytest.fail("approved skill scripts require the F13 container runner")


def _package(root: Path, helper: str) -> None:
    (root / "scripts").mkdir()
    (root / "scripts" / "helper.py").write_text(helper, encoding="utf-8")
    (root / "scripts" / "mark.py").write_text(
        "import os\nfrom pathlib import Path\nimport sys\nimport helper\n"
        "Path(sys.argv[1]).write_text(helper.VALUE + '|' + str(os.environ.get('AETHER_PROVIDER_KEY')), encoding='utf-8')\n",
        encoding="utf-8",
    )


def test_unapproved_script_does_not_execute(tmp_path: Path) -> None:
    _package(tmp_path, "VALUE = 'ok'\n")
    marker = tmp_path / "marker.txt"
    refused = execute_script(tmp_path, "mark.py", approved_hashes=set(), output=marker)
    assert refused["executed"] is False
    assert refused["reason"] == "unapproved"
    assert not marker.exists()


def test_tampered_sibling_is_rejected(tmp_path: Path) -> None:
    _require_docker()
    _package(tmp_path, "VALUE = 'ok'\n")
    approved = scripts_tree_sha256(_script_bytes(tmp_path))
    marker = tmp_path / "marker.txt"
    first = execute_script(tmp_path, "mark.py", approved_hashes={approved}, output=marker)
    assert first["executed"] is True
    assert marker.read_text(encoding="utf-8").startswith("ok|")
    (tmp_path / "scripts" / "helper.py").write_text("VALUE = 'tampered'\n", encoding="utf-8")
    tampered = tmp_path / "tampered.txt"
    refused = execute_script(tmp_path, "mark.py", approved_hashes={approved}, output=tampered)
    assert refused["executed"] is False
    assert refused["reason"] == "unapproved"
    assert not tampered.exists()


def test_host_secrets_are_not_visible(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    _require_docker()
    monkeypatch.setenv("AETHER_PROVIDER_KEY", "sk-live-host-secret")
    monkeypatch.setenv("AETHER_ADMIN_DATABASE_URL", "postgresql://secret")
    _package(tmp_path, "VALUE = 'ok'\n")
    approved = scripts_tree_sha256(_script_bytes(tmp_path))
    marker = tmp_path / "marker.txt"
    ran = execute_script(tmp_path, "mark.py", approved_hashes={approved}, output=marker)
    body = marker.read_text(encoding="utf-8")
    assert ran["executed"] is True
    assert "sk-live-host-secret" not in body
    assert "postgresql://secret" not in body
    assert os.environ["AETHER_PROVIDER_KEY"] not in body
    assert body == "ok|None"


def test_candidate_cannot_modify_trusted_control_code() -> None:
    target = Path(__file__).resolve().parents[2] / "services/worker/aether_worker/verification.py"
    before = target.read_bytes()
    with pytest.raises(SkillError) as denied:
        propose_candidate(
            {
                "auto_promote": False,
                "requested_capabilities": ["tool.read"],
                "edits": ["./services/worker/aether_worker/verification.py"],
            }
        )
    assert denied.value.code == "control_edit_rejected"
    assert target.read_bytes() == before
