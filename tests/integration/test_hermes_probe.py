import json
import os
from pathlib import Path

from aether_worker.hermes_baseline import probe_cli


def test_pinned_hermes_cli_prints_its_real_version(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    lock = json.loads(Path("config/hermes.lock.json").read_text(encoding="utf-8"))
    probed = probe_cli()
    assert probed["status"] == "probed", probed
    assert probed["returncode"] == 0
    lines = probed["version"].splitlines()
    assert lines[0].startswith(f"Hermes Agent v{lock['version']} ")
    assert lock["commit"][:8] in lines[0]
    assert "Install method: git" in lines
    assert any(line.startswith("Python: 3.14") for line in lines)
    assert f"OpenAI SDK: {lock['openai_sdk']}" in lines
    assert os.path.basename(probed["path"]) == "hermes"
