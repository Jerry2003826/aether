from pathlib import Path

from aether_worker.supply import _install_tree, build_sbom, restore_project


def test_sbom_uses_the_lockfile_and_does_not_call_an_empty_list_reviewed() -> None:
    bom = build_sbom(Path("."))
    names = {item["name"]: item["version"] for item in bom["components"] if item["source"] == "uv.lock"}
    assert names["fastapi"]
    assert names["aether"] == "1.4.0"
    assert bom["vulnerabilities"]["status"] == "not_run"
    assert bom["vulnerabilities"]["mode"] == "offline"
    source = Path("services/worker/aether_worker/supply.py").read_text(encoding="utf-8")
    assert "down -v" not in source
    assert "docker compose down" not in source


def test_artifact_replace_renames_a_finished_copy_into_place(tmp_path: Path) -> None:
    destination = tmp_path / "artifacts"
    destination.mkdir()
    (destination / "note.txt").write_text("live", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()
    (source / "note.txt").write_text("backup", encoding="utf-8")
    _install_tree(source, destination)
    assert (destination / "note.txt").read_text(encoding="utf-8") == "backup"
    assert list(tmp_path.glob(".artifacts.restore-*")) == []
    assert list(tmp_path.glob(".artifacts.previous-*")) == []


def test_artifact_replace_keeps_the_live_tree_when_the_copy_fails(tmp_path: Path, monkeypatch) -> None:
    destination = tmp_path / "artifacts"
    destination.mkdir()
    (destination / "note.txt").write_text("live", encoding="utf-8")
    source = tmp_path / "src"
    source.mkdir()

    def fail(*_args, **_kwargs):
        raise RuntimeError("copy failed")

    monkeypatch.setattr("aether_worker.supply.shutil.copytree", fail)
    try:
        _install_tree(source, destination)
    except RuntimeError as exc:
        assert str(exc) == "copy failed"
    else:
        raise AssertionError("copy failure was ignored")
    assert (destination / "note.txt").read_text(encoding="utf-8") == "live"


def test_restore_requires_confirmation(tmp_path: Path) -> None:
    try:
        restore_project("postgresql://invalid", tmp_path, tmp_path, confirm="no")
    except RuntimeError as exc:
        assert str(exc) == "restore_not_confirmed"
    else:
        raise AssertionError("restore ran without confirmation")
