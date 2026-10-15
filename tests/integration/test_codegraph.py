import hashlib
from pathlib import Path

import pytest

from aether_worker.codegraph_adapter import RELEASE, CodeGraphError, install_cli, CodeGraphAdapter

PROJECT = "00000000-0000-4000-8000-0000000000a1"
OTHER = "00000000-0000-4000-8000-0000000000b1"
REPOSITORY = "00000000-0000-4000-8000-0000000000a2"


@pytest.fixture(scope="module")
def binary(tmp_path_factory: pytest.TempPathFactory) -> Path:
    return install_cli(tmp_path_factory.mktemp("codegraph-prefix"))


def _source(root: Path) -> None:
    root.mkdir(parents=True)
    secret = root / "secrets"
    secret.mkdir()
    (secret / "keys.js").write_text('export const KEY = "UNIQUE_SECRET add"\n', encoding="utf-8")
    (root / ".env").write_text("TOKEN=UNIQUE_SECRET\n", encoding="utf-8")
    (root / "math.js").write_text("export function add(a, b) { return a + b }\n", encoding="utf-8")
    (root / "math.test.js").write_text('import { add } from "./math.js"\nexport function testAdd() { return add(1, 2) }\n', encoding="utf-8")


def test_product_gate_rejects_install_and_limits(binary: Path, tmp_path: Path) -> None:
    adapter = CodeGraphAdapter(binary, tmp_path / "home", max_files=1)
    assert adapter.product_enabled is False
    assert RELEASE == "1.6.2"
    with pytest.raises(CodeGraphError) as disabled:
        adapter.product_query()
    assert disabled.value.code == "product_disabled"
    with pytest.raises(CodeGraphError) as rejected:
        adapter._run(["install"])
    assert rejected.value.code == "command_rejected"
    assert adapter.commands == []
    crowded = tmp_path / "crowded"
    _source(crowded)
    with pytest.raises(CodeGraphError) as limited:
        adapter.bind(PROJECT, REPOSITORY, "snap-limit", crowded)
    assert limited.value.code == "too_many_files"
    linked = tmp_path / "linked"
    linked.mkdir()
    outside = tmp_path / "outside.txt"
    outside.write_text("host secret\n", encoding="utf-8")
    (linked / "escape.js").symlink_to(outside)
    with pytest.raises(CodeGraphError) as escaped:
        adapter.bind(PROJECT, REPOSITORY, "snap-link", linked)
    assert escaped.value.code == "symlink_escape"


def test_codegraph_release_snapshot_network_and_fallback(binary: Path, tmp_path: Path) -> None:
    trusted = tmp_path / "trusted_suite.py"
    trusted.write_text("def test_keep():\n    assert True\n", encoding="utf-8")
    trusted_hash = hashlib.sha256(trusted.read_bytes()).hexdigest()
    source = tmp_path / "origin"
    _source(source)
    adapter = CodeGraphAdapter(binary, tmp_path / "home")
    probed = adapter.probe_release()
    assert probed["version"] == RELEASE
    assert probed["telemetry"] == "off"
    assert probed["model_tools"] == ["codegraph_explore"]
    bound = adapter.bind(PROJECT, REPOSITORY, "snap-1", source)
    snapshot = Path(bound["path"])
    assert snapshot != source
    assert snapshot.is_relative_to(tmp_path / "home")
    assert not (snapshot / "secrets").exists()
    assert not (snapshot / ".env").exists()
    indexed = adapter.index(PROJECT, "snap-1")
    assert indexed["cold_index_seconds"] > 0
    assert indexed["cache_key"] == bound["cache_key"]
    found = adapter.explore(PROJECT, "snap-1", "add", actor_path=str(Path.home()))
    assert found["index_status"] == "ready"
    assert found["degraded_reason"] is None
    assert found["source_freshness_confirmed"] is True
    assert "add" in found["context_text"]
    assert "UNIQUE_SECRET" not in found["context_text"]
    assert any(item["qualified_name"] == "add" for item in found["symbols"])
    assert all(not item["relative_path"].startswith("/") for item in found["symbols"])
    changed = adapter.impact(PROJECT, "snap-1", "add")
    assert changed["completeness"] == "partial"
    assert any(item["qualified_name"] == "testAdd" for item in changed["possible_impacted_symbols"])
    affected = adapter.affected_tests(PROJECT, "snap-1", "math.js")
    assert affected["advisory_only"] is True
    assert "math.test.js" in affected["suggested_relative_test_paths"]
    assert trusted_hash == hashlib.sha256(trusted.read_bytes()).hexdigest()
    report = adapter.compare(PROJECT, "snap-1", "add")
    assert report["cold_index_seconds"] > 0
    assert report["graph_context_chars"] > 0
    assert report["fallback_context_chars"] > 0
    assert report["graph_status"] == "ready"
    assert report["fallback_status"] == "degraded"
    assert report["vendor_savings_claimed"] is False
    blocked = adapter.measure_blocked_network(PROJECT, "snap-1", "add", probe_host="1.1.1.1")  # NOSONAR
    assert blocked["network_errno"] == 101
    assert blocked["explore_ok"] is True
    assert blocked["telemetry"] == "off"
    with pytest.raises(CodeGraphError) as other:
        adapter.explore(OTHER, "snap-1", "add")
    assert other.value.code == "cross_project"
    (snapshot / "math.js").write_text("export function add(a, b) { return a - b }\n", encoding="utf-8")
    with pytest.raises(CodeGraphError) as stale:
        adapter.explore(PROJECT, "snap-1", "add")
    assert stale.value.code == "stale_snapshot"
    (snapshot / "math.js").write_text("export function add(a, b) { return a + b }\n", encoding="utf-8")
    (tmp_path / "home" / "secret-host.txt").write_text("add should not be found outside the snapshot\n", encoding="utf-8")
    degraded = adapter.fallback(PROJECT, "snap-1", "add")
    assert degraded["degraded_reason"] == "graph_unavailable"
    assert "math.js" in degraded["matches"]
    assert "secret-host.txt" not in degraded["matches"]
    assert all(not match.startswith("..") and not match.startswith("/") for match in degraded["matches"])
    secret_search = adapter.fallback(PROJECT, "snap-1", "UNIQUE_SECRET")
    assert secret_search["matches"] == []
    assert all(command[0] != "install" for command in adapter.commands)
    assert all("/etc" not in " ".join(command) and str(Path.home()) not in " ".join(command) for command in adapter.commands)
    assert adapter.invocations
    assert all(invocation[invocation.index("--network") + 1] == "none" for invocation in adapter.invocations)
    assert all("docker.sock" not in " ".join(invocation) for invocation in adapter.invocations)
    adapter.cancel(PROJECT, "snap-1")
    assert not snapshot.exists()
