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
    assert adapter.product_enabled is True
    posture = adapter.product_query()
    assert posture["telemetry"] == "off"
    assert posture["allow_network"] is False
    assert posture["mount_docker_socket"] is False
    assert posture["trust_graph_test_recommendations"] is False
    assert RELEASE == "1.6.2"
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


def _indexed(binary: Path, tmp_path: Path):
    trusted = tmp_path / "trusted_suite.py"
    trusted.write_text("def test_keep():\n    assert True\n", encoding="utf-8")
    source = tmp_path / "origin"
    _source(source)
    adapter = CodeGraphAdapter(binary, tmp_path / "home")
    bound = adapter.bind(PROJECT, REPOSITORY, "snap-1", source)
    indexed = adapter.index(PROJECT, "snap-1")
    return adapter, bound, Path(bound["path"]), trusted, indexed


def test_fixed_release_telemetry_and_blocked_network(binary: Path, tmp_path: Path) -> None:
    adapter, _bound, _snapshot, _trusted, _indexed_result = _indexed(binary, tmp_path)
    probed = adapter.probe_release()
    assert probed["version"] == RELEASE
    assert probed["telemetry"] == "off"
    assert probed["model_tools"] == ["codegraph_explore"]
    blocked = adapter.measure_blocked_network(PROJECT, "snap-1", "add", probe_host="1.1.1.1")  # NOSONAR
    assert blocked["network_errno"] == 101
    assert blocked["explore_ok"] is True
    assert blocked["telemetry"] == "off"
    assert adapter.invocations
    assert all(invocation[invocation.index("--network") + 1] == "none" for invocation in adapter.invocations)
    assert all("docker.sock" not in " ".join(invocation) for invocation in adapter.invocations)


def test_indexes_snapshot_copy_and_hash_expires(binary: Path, tmp_path: Path) -> None:
    adapter, bound, snapshot, _trusted, indexed = _indexed(binary, tmp_path)
    source = tmp_path / "origin"
    assert snapshot != source
    assert snapshot.is_relative_to(tmp_path / "home")
    assert not (snapshot / "secrets").exists()
    assert not (snapshot / ".env").exists()
    assert indexed["cold_index_seconds"] > 0
    assert indexed["cache_key"] == bound["cache_key"]
    (snapshot / "math.js").write_text("export function add(a, b) { return a - b }\n", encoding="utf-8")
    with pytest.raises(CodeGraphError) as stale:
        adapter.explore(PROJECT, "snap-1", "add")
    assert stale.value.code == "stale_snapshot"


def test_cross_project_rejected_and_actor_path_discarded(binary: Path, tmp_path: Path) -> None:
    adapter, _bound, _snapshot, _trusted, _indexed_result = _indexed(binary, tmp_path)
    found = adapter.explore(PROJECT, "snap-1", "add", actor_path=str(Path.home()))
    assert "add" in found["context_text"]
    assert all("/etc" not in " ".join(command) and str(Path.home()) not in " ".join(command) for command in adapter.commands)
    with pytest.raises(CodeGraphError) as other:
        adapter.explore(OTHER, "snap-1", "add")
    assert other.value.code == "cross_project"


def test_cancel_clears_snapshot_and_fallback_stays_inside(binary: Path, tmp_path: Path) -> None:
    adapter, _bound, snapshot, _trusted, _indexed_result = _indexed(binary, tmp_path)
    (tmp_path / "home" / "secret-host.txt").write_text("add should not be found outside the snapshot\n", encoding="utf-8")
    degraded = adapter.fallback(PROJECT, "snap-1", "add")
    assert degraded["degraded_reason"] == "graph_unavailable"
    assert "math.js" in degraded["matches"]
    assert "secret-host.txt" not in degraded["matches"]
    assert all(not match.startswith("..") and not match.startswith("/") for match in degraded["matches"])
    secret_search = adapter.fallback(PROJECT, "snap-1", "UNIQUE_SECRET")
    assert secret_search["matches"] == []
    adapter.cancel(PROJECT, "snap-1")
    assert not snapshot.exists()


def test_cold_index_and_graph_on_off_do_not_claim_vendor_savings(binary: Path, tmp_path: Path) -> None:
    adapter, _bound, _snapshot, _trusted, indexed = _indexed(binary, tmp_path)
    assert indexed["cold_index_seconds"] > 0
    report = adapter.compare(PROJECT, "snap-1", "add")
    assert report["cold_index_seconds"] > 0
    assert report["graph_context_chars"] > 0
    assert report["fallback_context_chars"] > 0
    assert report["graph_status"] == "ready"
    assert report["fallback_status"] == "degraded"
    assert report["vendor_savings_claimed"] is False


def test_trusted_suite_unchanged_and_affected_is_advisory(binary: Path, tmp_path: Path) -> None:
    adapter, _bound, _snapshot, trusted, _indexed_result = _indexed(binary, tmp_path)
    trusted_hash = hashlib.sha256(trusted.read_bytes()).hexdigest()
    affected = adapter.affected_tests(PROJECT, "snap-1", "math.js")
    assert affected["advisory_only"] is True
    assert "math.test.js" in affected["suggested_relative_test_paths"]
    assert trusted_hash == hashlib.sha256(trusted.read_bytes()).hexdigest()
    assert trusted.read_text(encoding="utf-8").count("def test_keep") == 1


def test_callers_and_callees_match_the_fixture_source(binary: Path, tmp_path: Path) -> None:
    adapter, _bound, snapshot, _trusted, _indexed_result = _indexed(binary, tmp_path)
    source = (snapshot / "math.test.js").read_text(encoding="utf-8")
    assert "return add(1, 2)" in source
    callers = adapter.callers(PROJECT, "snap-1", "add", relative_file="math.js")
    callees = adapter.callees(PROJECT, "snap-1", "testAdd", relative_file="math.test.js")
    assert callers["ambiguous"] is False
    assert any(row["name"] == "testAdd" and row["relative_path"] == "math.test.js" and "calls" in row["relationships"] for row in callers["rows"])
    assert any(row["name"] == "add" and row["relative_path"] == "math.js" and "calls" in row["relationships"] for row in callees["rows"])
