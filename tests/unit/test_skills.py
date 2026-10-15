from pathlib import Path

import pytest

from aether_worker.skills import (
    SkillError,
    _script_bytes,
    execute_script,
    load_body,
    load_metadata,
    load_resource,
    propose_candidate,
    read_split,
    redact_trace,
    scripts_tree_sha256,
)


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
    (tmp_path / "scripts" / "helper.py").write_text("VALUE = 'sibling'\n", encoding="utf-8")
    marker = tmp_path / "marker.txt"
    refused = execute_script(tmp_path, "mark.py", approved_hashes=set(), output=marker)
    assert refused["executed"] is False
    assert refused["reason"] == "unapproved"
    assert not marker.exists()
    assert refused["sha256"] == scripts_tree_sha256(_script_bytes(tmp_path))


def test_trace_replaces_the_whole_secret_token(tmp_path: Path) -> None:
    for split in ("train", "dev", "holdout"):
        (tmp_path / split).mkdir()
        (tmp_path / split / "trace.txt").write_text(
            f"{split} sk-live-abc123 api_key=super-secret 密钥正文\n",
            encoding="utf-8",
        )
    train = read_split(tmp_path, "train")
    direct = redact_trace("sk-live-abc123 api_key=super-secret 密钥正文")
    for redacted in (train, direct):
        assert "sk-live-abc123" not in redacted
        assert "abc123" not in redacted
        assert "super-secret" not in redacted
        assert "密钥正文" not in redacted
        assert "[REDACTED]" in redacted
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
    accepted = propose_candidate(
        {
            "auto_promote": False,
            "requested_capabilities": ["tool.read"],
            "edits": ["notes/runbook.md", "services/../notes/ok.md", "./notes/runbook.md"],
        }
    )
    assert accepted["status"] == "QUARANTINED"
    assert accepted["auto_promote"] is False
    for raw in (
        "./services/worker/aether_worker/verification.py",
        "notes/../../services/api/app.py",
        "/workspace/services/worker/aether_worker/verification.py",
        "Services/Worker/aether_worker/verification.py",
        "notes/../packages/domain/model.py",
    ):
        with pytest.raises(SkillError) as escaped:
            propose_candidate({"auto_promote": False, "requested_capabilities": ["tool.read"], "edits": [raw]})
        assert escaped.value.code == "control_edit_rejected"
