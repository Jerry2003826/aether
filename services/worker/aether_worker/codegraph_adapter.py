"""CodeGraph adapter. Product enablement stays off until an owner turns it on."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import platform
import shutil
import subprocess
import time
import uuid
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[3]
LOCK = json.loads((ROOT / "config" / "codegraph.lock.json").read_text(encoding="utf-8"))
RELEASE = str(LOCK["version"])
PACKAGE = str(LOCK["package"])
PLATFORM_PACKAGE = str(LOCK["platform_package"])
ALLOWED_COMMANDS = frozenset({"--version", "--help", "telemetry", "init", "explore", "impact", "affected", "query", "status", "callers", "callees"})
SKIP_DIRECTORIES = frozenset({".git", ".codegraph", "secrets", ".secrets", "credentials", "node_modules", "__pycache__", ".venv", ".next", ".cache"})
SYMBOL_KINDS = frozenset({"function", "method", "class", "module", "interface", "type", "other"})


class CodeGraphError(Exception):
    def __init__(self, code: str, detail: str = "") -> None:
        super().__init__(code if not detail else f"{code}: {detail}")
        self.code = code


def _sha512_integrity(path: Path) -> str:
    digest = hashlib.sha512(path.read_bytes()).digest()
    return "sha512-" + base64.b64encode(digest).decode("ascii")


def _require_platform() -> None:
    system = platform.system().lower()
    machine = platform.machine().lower()
    if system == "linux" and machine in {"x86_64", "amd64"} and PLATFORM_PACKAGE.endswith("linux-x64"):
        return
    raise CodeGraphError("unsupported_platform")


def _npm_env(home: Path) -> dict[str, str]:
    return {
        "PATH": os.environ.get("PATH", ""),
        "HOME": str(home),
        "npm_config_update_notifier": "false",
        "CODEGRAPH_TELEMETRY": "0",
        "DO_NOT_TRACK": "1",
        "CODEGRAPH_NO_DOWNLOAD": "1",
    }


def _pack(prefix: Path, spec: str, integrity: str) -> Path:
    completed = subprocess.run(
        ["npm", "pack", spec, "--pack-destination", str(prefix)],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=_npm_env(prefix),
    )
    filename = completed.stdout.strip().splitlines()[-1].strip() if completed.stdout.strip() else ""
    tarball = prefix / filename
    if completed.returncode != 0 or not tarball.is_file():
        raise CodeGraphError("install_failed")
    if _sha512_integrity(tarball) != integrity:
        raise CodeGraphError("integrity_mismatch")
    return tarball


def install_cli(prefix: Path) -> Path:
    """Install the pinned tarballs after checking npm sha512. Never runs `codegraph install`."""
    prefix.mkdir(parents=True, exist_ok=True)
    _require_platform()
    main = _pack(prefix, f"{PACKAGE}@{RELEASE}", str(LOCK["integrity"]))
    platform_tarball = _pack(prefix, f"{PLATFORM_PACKAGE}@{RELEASE}", str(LOCK["platform_integrity"]))
    installed = subprocess.run(
        ["npm", "install", "--omit=dev", "--ignore-scripts", "--prefix", str(prefix), str(main), str(platform_tarball)],
        capture_output=True,
        text=True,
        check=False,
        timeout=180,
        env=_npm_env(prefix),
    )
    binary = prefix / "node_modules" / ".bin" / "codegraph"
    launcher = prefix / "node_modules" / "@colbymchenry" / "codegraph-linux-x64" / "bin" / "codegraph"
    if installed.returncode != 0 or not binary.is_file() or not launcher.is_file():
        raise CodeGraphError("install_failed")
    return binary


def _tree_sha(root: Path) -> str:
    lines: list[str] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.is_symlink() or ".codegraph" in path.parts:
            continue
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest} {path.relative_to(root).as_posix()}")
    return hashlib.sha256("\n".join(lines).encode()).hexdigest()


def _uuid(value: str) -> str:
    try:
        return str(uuid.UUID(value))
    except ValueError as exc:
        raise CodeGraphError("bad_id") from exc


def _safe_name(value: str) -> str:
    if not value or value in {".", ".."} or "/" in value or "\\" in value:
        raise CodeGraphError("bad_id")
    return value


def _kind(value: str) -> str:
    if value in SYMBOL_KINDS:
        return value
    if value == "file":
        return "module"
    return "other"


def _relative(root: Path, raw: str) -> Path | None:
    if not raw or raw.startswith(("/", "\\")) or ".." in raw.split("/"):
        return None
    path = root / raw
    try:
        path.resolve().relative_to(root.resolve())
    except ValueError:
        return None
    if not path.is_file() or path.is_symlink():
        return None
    return path


def _symbol(node: dict[str, Any], root: Path) -> dict[str, Any] | None:
    relative = str(node.get("filePath") or "")
    path = _relative(root, relative)
    if path is None:
        return None
    start = int(node.get("startLine") or 1)
    end = int(node.get("endLine") or start)
    name = str(node.get("qualifiedName") or node.get("name") or relative)
    return {
        "symbol_id": str(node.get("id") or name)[:512],
        "kind": _kind(str(node.get("kind") or "other")),
        "qualified_name": name[:512],
        "relative_path": relative,
        "start_line": max(1, start),
        "end_line": max(1, end),
        "file_sha256": hashlib.sha256(path.read_bytes()).hexdigest(),
    }


class CodeGraphAdapter:
    def __init__(self, binary: Path, home: Path, *, max_files: int | None = None, max_context_chars: int | None = None) -> None:
        self.binary = binary
        self.home = home
        self.home.mkdir(parents=True, exist_ok=True)
        self.max_files = int(max_files if max_files is not None else LOCK["max_files"])
        self.max_context_chars = int(max_context_chars if max_context_chars is not None else LOCK["max_context_chars"])
        self.launcher = binary.resolve().parents[1] / "codegraph-linux-x64" / "bin" / "codegraph"
        self.bundle = self.launcher.parents[1]
        if not self.launcher.is_file() or not (self.bundle / "node").is_file():
            raise CodeGraphError("install_failed")
        self.commands: list[list[str]] = []
        self.invocations: list[list[str]] = []
        self._bound: dict[tuple[str, str], dict[str, Any]] = {}

    @property
    def product_enabled(self) -> bool:
        return bool(LOCK.get("product_enabled"))

    def product_query(self) -> dict[str, Any]:
        """Owner switch. The example template stays off; this lock is the installed switch."""
        if not self.product_enabled:
            raise CodeGraphError("product_disabled")
        return {
            "product_enabled": True,
            "telemetry": "off",
            "allow_network": False,
            "mount_docker_socket": False,
            "model_tools": ["codegraph_explore"],
            "trust_graph_test_recommendations": False,
            "fallback": "authorized_source_read",
        }

    def _client_env(self) -> dict[str, str]:
        return {"PATH": os.environ.get("PATH", ""), "HOME": str(self.home)}

    def _inside(self, path: Path) -> None:
        resolved = path.resolve()
        home = self.home.resolve()
        if resolved != home and home not in resolved.parents:
            raise CodeGraphError("path_escape")

    def _ensure_image(self) -> None:
        image = str(LOCK["runner_image"])
        inspected = subprocess.run(["docker", "image", "inspect", image], capture_output=True, text=True, check=False, timeout=30, env=self._client_env())
        if inspected.returncode == 0:
            return
        pulled = subprocess.run(["docker", "pull", image], capture_output=True, text=True, check=False, timeout=180, env=self._client_env())
        if pulled.returncode != 0:
            raise CodeGraphError("runner_image_missing", (pulled.stderr or "")[-200:])

    def _mounts(self, args: list[str]) -> list[Path]:
        home = self.home.resolve()
        mounts: list[Path] = []
        for arg in args:
            if not arg.startswith("/"):
                continue
            candidate = Path(arg)
            if not candidate.exists():
                continue
            resolved = candidate.resolve()
            directory = resolved if resolved.is_dir() else resolved.parent
            if directory != home and home not in directory.parents:
                continue
            if directory not in mounts:
                mounts.append(directory)
        return mounts

    def _docker(self, argv: list[str], mounts: list[tuple[Path, str]]) -> subprocess.CompletedProcess[str]:
        self._ensure_image()
        command = [
            "docker", "run", "--rm",
            "--network", "none",
            "--read-only",
            "--cap-drop", "ALL",
            "--security-opt", "no-new-privileges",
            "--pids-limit", "64",
            "--user", f"{os.getuid()}:{os.getgid()}",
            "--tmpfs", "/run/aether:rw,nosuid,nodev,mode=0700",
            "-e", "HOME=/run/aether",
            "-e", "PATH=/usr/bin:/bin",
            "-e", "LANG=C.UTF-8",
            "-e", "CODEGRAPH_TELEMETRY=0",
            "-e", "DO_NOT_TRACK=1",
            "-e", "CODEGRAPH_NO_DOWNLOAD=1",
            "-e", "NO_COLOR=1",
        ]
        for path, mode in mounts:
            if mode not in {"ro", "rw"} or "docker.sock" in path.as_posix():
                raise CodeGraphError("socket_mount_rejected")
            command.extend(["-v", f"{path}:{path}:{mode}"])
        command.append(str(LOCK["runner_image"]))
        command.extend(argv)
        self.invocations.append(command)
        completed = subprocess.run(
            command,
            capture_output=True,
            text=True,
            check=False,
            timeout=int(LOCK["max_wall_seconds"]),
            env=self._client_env(),
        )
        if len(completed.stdout) > 1_000_000:
            raise CodeGraphError("output_limit")
        return completed

    def _run(self, args: list[str]) -> subprocess.CompletedProcess[str]:
        if not args or args[0] not in ALLOWED_COMMANDS:
            raise CodeGraphError("command_rejected")
        self.commands.append(list(args))
        mounts = [(self.bundle, "ro"), *[(path, "rw") for path in self._mounts(args)]]
        return self._docker([str(self.launcher), *args], mounts)

    def _copy_snapshot(self, source: Path, destination: Path) -> None:
        copied = 0
        source_root = source.resolve()
        for path in sorted(source.rglob("*")):
            relative = path.relative_to(source)
            if any(part in SKIP_DIRECTORIES or part.startswith(".env") for part in relative.parts):
                continue
            if path.is_symlink():
                try:
                    path.resolve().relative_to(source_root)
                except ValueError as exc:
                    raise CodeGraphError("symlink_escape") from exc
            if not path.is_file():
                continue
            copied += 1
            if copied > self.max_files:
                raise CodeGraphError("too_many_files")
            target = destination / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(path, target, follow_symlinks=False)

    def bind(self, project_id: str, repository_artifact_id: str, snapshot_id: str, source: Path) -> dict[str, Any]:
        project = _uuid(project_id)
        repository = _uuid(repository_artifact_id)
        snapshot = _safe_name(snapshot_id)
        destination = self.home / "snapshots" / project / snapshot
        self._inside(destination)
        if destination.exists():
            shutil.rmtree(destination)
        destination.mkdir(parents=True)
        try:
            self._copy_snapshot(source, destination)
        except CodeGraphError:
            shutil.rmtree(destination, ignore_errors=True)
            raise
        digest = _tree_sha(destination)
        options = json.dumps({"max_files": self.max_files, "max_context_chars": self.max_context_chars, "telemetry": "0"}, sort_keys=True)
        options_hash = hashlib.sha256(options.encode()).hexdigest()
        cache_key = hashlib.sha256(f"{project}|{repository}|{digest}|{RELEASE}|{options_hash}".encode()).hexdigest()
        self._bound[(project, snapshot)] = {
            "path": destination,
            "digest": digest,
            "project_id": project,
            "repository_artifact_id": repository,
            "indexed": False,
            "cold_index_seconds": 0.0,
            "cache_key": cache_key,
        }
        return {"snapshot_sha256": digest, "cache_key": cache_key, "path": str(destination)}

    def _record(self, project_id: str, snapshot_id: str) -> dict[str, Any]:
        record = self._bound.get((_uuid(project_id), _safe_name(snapshot_id)))
        if record is None:
            raise CodeGraphError("cross_project")
        if _tree_sha(record["path"]) != record["digest"]:
            raise CodeGraphError("stale_snapshot")
        return record

    def _base(self, record: dict[str, Any], status: str) -> dict[str, Any]:
        return {
            "project_id": record["project_id"],
            "repository_artifact_id": record["repository_artifact_id"],
            "snapshot_sha256": record["digest"],
            "codegraph_version": RELEASE,
            "index_status": status,
        }

    def probe_release(self) -> dict[str, Any]:
        version = self._run(["--version"])
        help_text = self._run(["--help"])
        telemetry = self._run(["telemetry", "status"])
        visible = ("explore", "impact", "affected")
        version_line = version.stdout.strip().splitlines()[-1].strip() if version.stdout.strip() else ""
        if version.returncode != 0 or version_line != RELEASE:
            raise CodeGraphError("release_mismatch", (version.stderr or version.stdout)[-300:])
        if help_text.returncode != 0 or any(name not in help_text.stdout for name in visible):
            raise CodeGraphError("schema_mismatch", (help_text.stderr or "")[-300:])
        if "Telemetry: disabled" not in telemetry.stdout:
            raise CodeGraphError("telemetry_enabled", (telemetry.stderr or telemetry.stdout)[-300:])
        return {"version": RELEASE, "telemetry": "off", "model_tools": ["codegraph_explore"]}

    def index(self, project_id: str, snapshot_id: str) -> dict[str, Any]:
        record = self._record(project_id, snapshot_id)
        started = time.perf_counter()
        completed = self._run(["init", "-y", str(record["path"])])
        elapsed = time.perf_counter() - started
        if completed.returncode != 0:
            raise CodeGraphError("index_failed", (completed.stderr or completed.stdout)[-300:])
        record["indexed"] = True
        record["cold_index_seconds"] = elapsed
        return {"cold_index_seconds": elapsed, "telemetry": "off", "cache_key": record["cache_key"]}

    def explore(self, project_id: str, snapshot_id: str, query: str, *, actor_path: str | None = None) -> dict[str, Any]:
        del actor_path
        record = self._record(project_id, snapshot_id)
        if not record["indexed"]:
            raise CodeGraphError("not_indexed")
        self._inside(record["path"])
        explored = self._run(["explore", "--path", str(record["path"]), "--max-files", "2", query])
        listed = self._run(["query", "--path", str(record["path"]), "--json", "--limit", "20", query])
        if explored.returncode != 0 or listed.returncode != 0:
            detail = explored.stderr or listed.stderr or explored.stdout or listed.stdout
            raise CodeGraphError("explore_failed", detail[-300:])
        try:
            rows = json.loads(listed.stdout)
        except json.JSONDecodeError as exc:
            raise CodeGraphError("bad_cli_output") from exc
        symbols = [item for item in (_symbol(row.get("node") or {}, record["path"]) for row in rows) if item is not None]
        payload = self._base(record, "ready")
        payload.update({
            "query": query[:512],
            "symbols": symbols[:100],
            "context_text": explored.stdout[: self.max_context_chars],
            "source_freshness_confirmed": True,
            "degraded_reason": None,
        })
        return payload

    def impact(self, project_id: str, snapshot_id: str, symbol: str) -> dict[str, Any]:
        record = self._record(project_id, snapshot_id)
        if not record["indexed"]:
            raise CodeGraphError("not_indexed")
        completed = self._run(["impact", "--path", str(record["path"]), "--json", symbol])
        if completed.returncode != 0:
            raise CodeGraphError("impact_failed", (completed.stderr or completed.stdout)[-300:])
        try:
            body = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CodeGraphError("bad_cli_output") from exc
        impacted = [item for item in (_symbol(node, record["path"]) for node in body.get("affected") or []) if item is not None]
        payload = self._base(record, "ready")
        payload.update({"symbol_ref": symbol[:512], "possible_impacted_symbols": impacted[:100], "completeness": "partial"})
        return payload

    def _symbol_arg(self, value: str) -> str:
        if not value or value.startswith("-") or any(char in value for char in "\n\r") or len(value) > 200:
            raise CodeGraphError("bad_id")
        return value

    def _relation(self, command: str, project_id: str, snapshot_id: str, symbol: str, *, relative_file: str | None) -> dict[str, Any]:
        record = self._record(project_id, snapshot_id)
        if not record["indexed"]:
            raise CodeGraphError("not_indexed")
        if relative_file is not None and _relative(record["path"], relative_file) is None:
            raise CodeGraphError("bad_id")
        args = [command, "--path", str(record["path"]), "--json", "--limit", "50"]
        if relative_file is not None:
            args.extend(["--file", relative_file])
        args.append(self._symbol_arg(symbol))
        completed = self._run(args)
        if completed.returncode != 0:
            raise CodeGraphError(f"{command}_failed", (completed.stderr or completed.stdout)[-300:])
        try:
            body = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CodeGraphError("bad_cli_output") from exc
        if not isinstance(body, dict):
            raise CodeGraphError("bad_cli_output")
        rows = []
        for row in body.get(command) or []:
            if not isinstance(row, dict):
                continue
            relative = str(row.get("filePath") or "")
            if _relative(record["path"], relative) is None:
                continue
            rows.append(
                {
                    "name": str(row.get("name") or "")[:200],
                    "kind": str(row.get("kind") or ""),
                    "relative_path": relative,
                    "start_line": int(row.get("startLine") or 0),
                    "relationships": [str(item) for item in row.get("relationships") or []],
                }
            )
        return {
            "symbol": symbol,
            "ambiguous": bool(body.get("ambiguous")),
            "truncated": bool(body.get("truncated")),
            "rows": rows,
        }

    def callers(self, project_id: str, snapshot_id: str, symbol: str, *, relative_file: str | None = None) -> dict[str, Any]:
        return self._relation("callers", project_id, snapshot_id, symbol, relative_file=relative_file)

    def callees(self, project_id: str, snapshot_id: str, symbol: str, *, relative_file: str | None = None) -> dict[str, Any]:
        return self._relation("callees", project_id, snapshot_id, symbol, relative_file=relative_file)

    def index_status(self, project_id: str, snapshot_id: str) -> dict[str, Any]:
        record = self._record(project_id, snapshot_id)
        completed = self._run(["status", "--json", str(record["path"])])
        if completed.returncode != 0:
            raise CodeGraphError("status_failed", (completed.stderr or completed.stdout)[-300:])
        try:
            body = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CodeGraphError("bad_cli_output") from exc
        if not isinstance(body, dict):
            raise CodeGraphError("bad_cli_output")
        return {
            "file_count": body.get("fileCount"),
            "node_count": body.get("nodeCount"),
            "edge_count": body.get("edgeCount"),
            "db_size_bytes": body.get("dbSizeBytes"),
            "state": (body.get("index") or {}).get("state") if isinstance(body.get("index"), dict) else None,
            "version": body.get("version"),
            "cold_index_seconds": record["cold_index_seconds"],
        }

    def affected_tests(self, project_id: str, snapshot_id: str, filename: str) -> dict[str, Any]:
        record = self._record(project_id, snapshot_id)
        if not record["indexed"]:
            raise CodeGraphError("not_indexed")
        completed = self._run(["affected", "--path", str(record["path"]), "--json", filename])
        if completed.returncode != 0:
            raise CodeGraphError("affected_failed", (completed.stderr or completed.stdout)[-300:])
        try:
            body = json.loads(completed.stdout)
        except json.JSONDecodeError as exc:
            raise CodeGraphError("bad_cli_output") from exc
        paths = [item for item in body.get("affectedTests") or [] if _relative(record["path"], str(item)) is not None]
        payload = self._base(record, "ready")
        payload.update({"suggested_relative_test_paths": paths[:250], "advisory_only": True})
        return payload

    def fallback(self, project_id: str, snapshot_id: str, query: str) -> dict[str, Any]:
        record = self._record(project_id, snapshot_id)
        matches: list[str] = []
        excerpts: list[str] = []
        for path in sorted(record["path"].rglob("*")):
            if not path.is_file() or path.is_symlink() or ".codegraph" in path.parts:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            if query not in text:
                continue
            relative = path.relative_to(record["path"]).as_posix()
            matches.append(relative)
            excerpts.append(f"{relative}\n{text[:500]}")
        payload = self._base(record, "degraded")
        payload.update({
            "query": query[:512],
            "symbols": [],
            "context_text": "\n".join(excerpts)[: self.max_context_chars],
            "source_freshness_confirmed": True,
            "degraded_reason": "graph_unavailable",
            "matches": matches,
        })
        return payload

    def compare(self, project_id: str, snapshot_id: str, query: str) -> dict[str, Any]:
        graph = self.explore(project_id, snapshot_id, query)
        degraded = self.fallback(project_id, snapshot_id, query)
        record = self._record(project_id, snapshot_id)
        return {
            "cold_index_seconds": record["cold_index_seconds"],
            "graph_context_chars": len(str(graph["context_text"])),
            "fallback_context_chars": len(str(degraded["context_text"])),
            "graph_status": graph["index_status"],
            "fallback_status": degraded["index_status"],
            "vendor_savings_claimed": False,
        }

    def measure_blocked_network(self, project_id: str, snapshot_id: str, query: str, *, probe_host: str) -> dict[str, Any]:
        self._record(project_id, snapshot_id)
        if not probe_host or probe_host.startswith("-") or any(char.isspace() for char in probe_host):
            raise CodeGraphError("bad_probe")
        script = (
            "const net=require('net');"
            "const socket=net.connect(443, process.argv[1]);"
            "socket.setTimeout(2000);"
            "const done=(payload)=>{process.stdout.write(JSON.stringify(payload));process.exit(0)};"
            "socket.on('error',(error)=>done({errno:error.errno,code:error.code}));"
            "socket.on('timeout',()=>done({errno:'timeout'}));"
        )
        probed = self._docker([str(self.bundle / "node"), "--liftoff-only", "-e", script, probe_host], [(self.bundle, "ro")])
        if probed.returncode != 0:
            raise CodeGraphError("network_probe_failed", (probed.stderr or probed.stdout)[-300:])
        try:
            payload = json.loads(probed.stdout.strip().splitlines()[-1])
        except json.JSONDecodeError as exc:
            raise CodeGraphError("network_probe_failed") from exc
        errno = payload.get("errno")
        number = abs(errno) if isinstance(errno, int) else 0
        found = self.explore(project_id, snapshot_id, query)
        return {
            "network_errno": number,
            "explore_ok": found["index_status"] == "ready" and query in str(found["context_text"]),
            "telemetry": "off",
        }

    def cancel(self, project_id: str, snapshot_id: str) -> None:
        record = self._bound.pop((_uuid(project_id), _safe_name(snapshot_id)), None)
        if record is None:
            raise CodeGraphError("cross_project")
        shutil.rmtree(record["path"], ignore_errors=True)
