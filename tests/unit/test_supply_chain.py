from pathlib import Path

from aether_worker.supply import build_sbom, restore_project


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


def test_restore_requires_confirmation(tmp_path: Path) -> None:
    try:
        restore_project("postgresql://invalid", tmp_path, tmp_path, confirm="no")
    except RuntimeError as exc:
        assert str(exc) == "restore_not_confirmed"
    else:
        raise AssertionError("restore ran without confirmation")
