import hashlib
from pathlib import Path

import pytest

from aether_worker.skills import SkillError, execute_script, load_body, load_metadata, load_resource, propose_candidate, read_split


def _package(root: Path) -> None:
    (root / "SKILL.md").write_text(
        "---\nname: check-lock\ndescription: 先核对锁文件\n---\nBODY_SECRET 步骤\n",
        encoding="utf-8",
    )
    (root / "references").mkdir()
    (root / "references" / "note.md").write_text("RESOURCE_MARKER\n", encoding="utf-8")
    (root / "scripts").mkdir()
    (root / "scripts" / "mark.py").write_text(
        "import pathlib, sys\npathlib.Path(sys.argv[1]).write_text('ran', encoding='utf-8')\n",
        encoding="utf-8",
    )


def test_metadata_body_and_resources_load_on_demand(tmp_path: Path) -> None:
    _package(tmp_path)
    meta = load_metadata(tmp_path)
    assert meta["name"] == "check-lock"
    assert meta["description"] == "先核对锁文件"
    assert meta["loaded"] == ["metadata"]
    assert "BODY_SECRET" not in str(meta)
    assert "RESOURCE_MARKER" not in str(meta)
    assert "BODY_SECRET" in load_body(tmp_path)
    assert load_resource(tmp_path, "references/note.md") == "RESOURCE_MARKER\n"


def test_unapproved_script_does_not_execute(tmp_path: Path) -> None:
    _package(tmp_path)
    marker = tmp_path / "marker.txt"
    refused = execute_script(tmp_path, "mark.py", approved_hashes=set(), output=marker)
    assert refused["executed"] is False
    assert refused["reason"] == "unapproved"
    assert not marker.exists()
    approved = execute_script(tmp_path, "mark.py", approved_hashes={refused["sha256"]}, output=marker)
    assert approved["executed"] is True
    assert marker.read_text(encoding="utf-8") == "ran"
    assert approved["sha256"] == hashlib.sha256((tmp_path / "scripts" / "mark.py").read_bytes()).hexdigest()


def test_trace_is_redacted_and_holdout_is_unreadable(tmp_path: Path) -> None:
    for split in ("train", "dev", "holdout"):
        (tmp_path / split).mkdir()
        (tmp_path / split / "trace.txt").write_text(f"{split} sk-live-secret 密钥\n", encoding="utf-8")
    train = read_split(tmp_path, "train")
    dev = read_split(tmp_path, "dev")
    assert "sk-live-secret" not in train
    assert "密钥" not in dev
    assert "[REDACTED]" in train
    with pytest.raises(SkillError) as denied:
        read_split(tmp_path, "holdout")
    assert denied.value.code == "holdout_rejected"


def test_candidate_cannot_auto_promote_or_edit_control_code() -> None:
    with pytest.raises(SkillError) as privilege:
        propose_candidate({"auto_promote": True, "requested_capabilities": ["tool.read"], "edits": ["notes/runbook.md"]})
    assert privilege.value.code == "auto_privilege_rejected"
    with pytest.raises(SkillError) as control:
        propose_candidate({"auto_promote": False, "requested_capabilities": ["tool.read"], "edits": ["services/worker/aether_worker/verification.py"]})
    assert control.value.code == "control_edit_rejected"
    accepted = propose_candidate({"auto_promote": False, "requested_capabilities": ["tool.read"], "edits": ["notes/runbook.md"]})
    assert accepted["status"] == "QUARANTINED"
    assert accepted["auto_promote"] is False
