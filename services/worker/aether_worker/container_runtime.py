"""Docker SDK calls. The argv shape stays so existing callers and the runner action set do not change."""

from __future__ import annotations

import io
import subprocess
from pathlib import Path
from typing import Any

import docker
import requests
from docker.errors import BuildError, DockerException, ImageNotFound, NotFound
from docker.utils import parse_repository_tag

_CLIENT: object | None = None
_LOG_STREAM_LIMIT = 65536


def set_docker_client(client: object | None) -> None:
    global _CLIENT
    _CLIENT = client


def docker_client() -> Any:
    if _CLIENT is not None:
        return _CLIENT
    return docker.from_env()


def execute_docker(args: list[str], *, timeout: float | None = None) -> subprocess.CompletedProcess[str]:
    """Translate one docker CLI invocation into a docker-py call. There is no subprocess fallback."""
    if not args:
        return _failed("empty")
    command, rest = args[0], args[1:]
    try:
        if command == "info":
            return _info(rest)
        if command == "ps":
            return _ps(rest)
        if command == "rm":
            return _rm(rest)
        if command == "stop":
            return _stop(rest)
        if command == "start":
            return _start(rest)
        if command == "inspect":
            return _inspect(rest)
        if command == "image" and rest[:1] == ["inspect"]:
            return _image_inspect(rest[1:])
        if command == "images":
            return _images(rest)
        if command == "pull":
            return _pull(rest)
        if command == "run":
            return _run(rest, timeout=timeout)
        if command == "exec":
            return _exec(rest)
        if command == "kill":
            return _kill(rest)
        if command == "logs":
            stdout, stderr = container_logs(rest[-1])
            return _ok(stdout.decode("utf-8", "replace"), stderr.decode("utf-8", "replace"))
    except subprocess.TimeoutExpired:
        raise
    except DockerException as exc:
        return _failed(str(exc))
    return _failed(f"unsupported:{command}")


def container_logs(name: str, *, limit: int = _LOG_STREAM_LIMIT) -> tuple[bytes, bytes]:
    """Stream logs and keep only the last `limit` bytes of each stream."""
    client = docker_client()
    try:
        container = client.containers.get(name)
    except (NotFound, DockerException):
        return b"", b""
    stdout = _tail_stream(container.logs(stdout=True, stderr=False, stream=True), limit)
    stderr = _tail_stream(container.logs(stdout=False, stderr=True, stream=True), limit)
    return stdout, stderr


def image_id_with_label(label: str) -> str | None:
    client = docker_client()
    images = client.images.list(filters={"label": [label]})
    if not images:
        return None
    return str(images[0].id)


def build_from_dockerfile(dockerfile: str, *, tag: str, labels: dict[str, str]) -> subprocess.CompletedProcess[str]:
    client = docker_client()
    try:
        client.images.build(fileobj=io.BytesIO(dockerfile.encode()), tag=tag, labels=labels, rm=True)
    except (BuildError, DockerException) as exc:
        return _failed(str(exc))
    return _ok("", "")


def build_context(*, root: Path, dockerfile: str, tag: str, labels: dict[str, str]) -> str:
    client = docker_client()
    image, _logs = client.images.build(path=str(root), dockerfile=dockerfile, tag=tag, labels=labels, rm=True)
    return str(image.id)


def _info(args: list[str]) -> subprocess.CompletedProcess[str]:
    payload = docker_client().info()
    version = str(payload.get("ServerVersion") or "")
    if args[:2] == ["--format", "{{.ServerVersion}}"]:
        if not version:
            return _failed("no_server_version")
        return _ok(version + "\n", "")
    return _ok("", "")


def _ps(args: list[str]) -> subprocess.CompletedProcess[str]:
    labels = [item.removeprefix("label=") for item in _flags(args, "--filter")]
    filters = {"label": labels} if labels else {}
    found = docker_client().containers.list(all=True, filters=filters)
    lines = [str(container.id) for container in found]
    text = "\n".join(lines)
    return _ok(text + ("\n" if text else ""), "")


def _rm(args: list[str]) -> subprocess.CompletedProcess[str]:
    name = _positional(args, skip={"-f"})
    try:
        docker_client().containers.get(name).remove(force=True)
    except NotFound:
        return _ok("", "")
    return _ok("", "")


def _stop(args: list[str]) -> subprocess.CompletedProcess[str]:
    grace = _option(args, "-t") or "10"
    name = _positional(args, skip={"-t", grace})
    try:
        docker_client().containers.get(name).stop(timeout=int(grace))
    except NotFound:
        return _failed("missing")
    return _ok("", "")


def _start(args: list[str]) -> subprocess.CompletedProcess[str]:
    try:
        docker_client().containers.get(args[-1]).start()
    except NotFound:
        return _failed("missing")
    return _ok("", "")


def _inspect(args: list[str]) -> subprocess.CompletedProcess[str]:
    template = ""
    name = args[-1]
    if args[:1] == ["-f"]:
        template = args[1]
    try:
        container = docker_client().containers.get(name)
        container.reload()
    except NotFound:
        return _failed("missing")
    state = container.attrs.get("State") or {}
    rendered = _state_template(template, state, str(container.attrs.get("Id") or container.id))
    return _ok(rendered + "\n", "")


def _image_inspect(args: list[str]) -> subprocess.CompletedProcess[str]:
    template = ""
    reference = args[-1]
    if "--format" in args:
        template = args[args.index("--format") + 1]
    try:
        image = docker_client().images.get(reference)
    except (ImageNotFound, NotFound):
        return _failed("missing")
    if template == "{{.Id}}":
        return _ok(str(image.id) + "\n", "")
    return _ok("", "")


def _images(args: list[str]) -> subprocess.CompletedProcess[str]:
    label = ""
    for item in _flags(args, "--filter"):
        if item.startswith("label="):
            label = item.removeprefix("label=")
    found = docker_client().images.list(filters={"label": [label]} if label else None)
    lines = [str(image.id) for image in found]
    text = "\n".join(lines)
    return _ok(text + ("\n" if text else ""), "")


def _pull(args: list[str]) -> subprocess.CompletedProcess[str]:
    if not args:
        return _failed("missing_image")
    repository, tag = parse_repository_tag(args[0])
    if tag is None:
        docker_client().images.pull(repository)
    else:
        docker_client().images.pull(repository, tag=tag)
    return _ok("", "")


def _run(args: list[str], *, timeout: float | None) -> subprocess.CompletedProcess[str]:
    parsed = _parse_run(args)
    kwargs = {key: value for key, value in parsed["kwargs"].items() if value is not None}
    container = docker_client().containers.run(parsed["image"], parsed["command"], detach=True, **kwargs)
    if parsed["detach"]:
        return _ok(str(container.id) + "\n", "")
    try:
        try:
            if timeout is None:
                status = container.wait()
            else:
                status = container.wait(timeout=timeout)
        except (DockerException, requests.exceptions.RequestException) as exc:
            if timeout is not None:
                try:
                    container.kill()
                except (DockerException, requests.exceptions.RequestException):
                    pass
                raise subprocess.TimeoutExpired(["docker", "run", parsed["image"]], timeout) from exc
            return _failed(str(exc))
        stdout = _as_bytes(container.logs(stdout=True, stderr=False) or b"").decode("utf-8", "replace")
        stderr = _as_bytes(container.logs(stdout=False, stderr=True) or b"").decode("utf-8", "replace")
        code = int((status or {}).get("StatusCode", 1))
        return subprocess.CompletedProcess(["docker", "run"], code, stdout, stderr)
    finally:
        if parsed["remove"]:
            try:
                container.remove(force=True)
            except DockerException:
                pass


def _exec(args: list[str]) -> subprocess.CompletedProcess[str]:
    environment: dict[str, str] = {}
    user = None
    workdir = None
    index = 0
    name = ""
    command: list[str] = []
    while index < len(args):
        token = args[index]
        if token == "-e":
            index += 1
            if index >= len(args):
                return _failed("missing_env")
            key, separator, value = args[index].partition("=")
            if separator != "=" or not key:
                return _failed("missing_env")
            environment[key] = value
        elif token in {"-u", "-w"}:
            index += 1
            if index >= len(args):
                return _failed("missing_option")
            if token == "-u":
                user = args[index]
            else:
                workdir = args[index]
        elif token in {"-i", "-t", "-d"}:
            pass
        elif token.startswith("-"):
            raise DockerException(f"unsupported flag {token}")
        else:
            name = token
            command = args[index + 1 :]
            break
        index += 1
    if not name or not command:
        return _failed("missing")
    try:
        container = docker_client().containers.get(name)
    except NotFound:
        return _failed("missing")
    kwargs: dict[str, Any] = {"demux": True, "stdout": True, "stderr": True}
    if environment:
        kwargs["environment"] = environment
    if user is not None:
        kwargs["user"] = user
    if workdir is not None:
        kwargs["workdir"] = workdir
    exit_code, output = container.exec_run(command, **kwargs)
    stdout_raw, stderr_raw = output if isinstance(output, tuple) else (output, b"")
    stdout = _as_bytes(stdout_raw or b"").decode("utf-8", "replace")
    stderr = _as_bytes(stderr_raw or b"").decode("utf-8", "replace")
    return subprocess.CompletedProcess(["docker", "exec"], int(exit_code), stdout, stderr)


def _kill(args: list[str]) -> subprocess.CompletedProcess[str]:
    signal = _option(args, "-s")
    skip = {"-s"}
    if signal is not None:
        skip.add(signal)
    name = _positional(args, skip=skip)
    try:
        container = docker_client().containers.get(name)
        if signal is None:
            container.kill()
        else:
            container.kill(signal=signal)
    except NotFound:
        return _failed("missing")
    return _ok("", "")


def _parse_run(args: list[str]) -> dict[str, Any]:
    detach = False
    remove = False
    name = None
    network = None
    read_only = None
    cap_drop: list[str] = []
    cap_add: list[str] = []
    security_opt: list[str] = []
    pids_limit = None
    mem_limit = None
    memswap_limit = None
    nano_cpus = None
    tmpfs: dict[str, str] = {}
    user = None
    environment: dict[str, str] = {}
    volumes: dict[str, dict[str, str]] = {}
    labels: dict[str, str] = {}
    index = 0
    image = ""
    command: list[str] = []
    while index < len(args):
        token = args[index]
        if token == "-d":
            detach = True
        elif token == "--rm":
            remove = True
        elif token == "--read-only":
            read_only = True
        elif token in {"--name", "--network", "--cap-drop", "--cap-add", "--security-opt", "--pids-limit", "--memory", "--memory-swap", "--cpus", "--user", "--label"}:
            index += 1
            value = args[index]
            if token == "--name":
                name = value
            elif token == "--network":
                network = value
            elif token == "--cap-drop":
                cap_drop.append(value)
            elif token == "--cap-add":
                cap_add.append(value)
            elif token == "--security-opt":
                security_opt.append(value)
            elif token == "--pids-limit":
                pids_limit = int(value)
            elif token == "--memory":
                mem_limit = int(value)
            elif token == "--memory-swap":
                memswap_limit = int(value)
            elif token == "--cpus":
                nano_cpus = int(float(value) * 1_000_000_000)
            elif token == "--user":
                user = value
            else:
                key, _, label_value = value.partition("=")
                labels[key] = label_value
        elif token == "-e":
            index += 1
            key, _, value = args[index].partition("=")
            environment[key] = value
        elif token == "-v":
            index += 1
            host, container_path, mode = _volume(args[index])
            volumes[host] = {"bind": container_path, "mode": mode}
        elif token == "--tmpfs":
            index += 1
            path, _, options = args[index].partition(":")
            tmpfs[path] = options
        elif token.startswith("-"):
            raise DockerException(f"unsupported flag {token}")
        else:
            image = token
            command = args[index + 1 :]
            break
        index += 1
    return {
        "image": image,
        "command": command or None,
        "detach": detach,
        "remove": remove,
        "kwargs": {
            "name": name,
            "network_mode": network,
            "read_only": read_only,
            "cap_drop": cap_drop or None,
            "cap_add": cap_add or None,
            "security_opt": security_opt or None,
            "pids_limit": pids_limit,
            "mem_limit": mem_limit,
            "memswap_limit": memswap_limit,
            "nano_cpus": nano_cpus,
            "tmpfs": tmpfs or None,
            "user": user,
            "environment": environment or None,
            "volumes": volumes or None,
            "labels": labels or None,
        },
    }


def _volume(spec: str) -> tuple[str, str, str]:
    host, container_path, mode = (spec.split(":", 2) + ["rw"])[:3]
    return host, container_path, mode or "rw"


def _state_template(template: str, state: dict[str, Any], image_id: str) -> str:
    if template == "{{.State.Running}}":
        return "true" if state.get("Running") else "false"
    if template == "{{.State.Status}}":
        return str(state.get("Status") or "")
    if template == "{{.State.ExitCode}}":
        return str(state.get("ExitCode"))
    if template == "{{.Id}}":
        return image_id
    if template:
        return ""
    return str(state.get("Status") or "")


def _flags(args: list[str], name: str) -> list[str]:
    found: list[str] = []
    index = 0
    while index < len(args):
        if args[index] == name and index + 1 < len(args):
            found.append(args[index + 1])
            index += 2
            continue
        index += 1
    return found


def _option(args: list[str], name: str) -> str | None:
    found = _flags(args, name)
    return found[-1] if found else None


def _positional(args: list[str], *, skip: set[str]) -> str:
    index = 0
    while index < len(args):
        token = args[index]
        if token in {"-t"}:
            index += 2
            continue
        if token in skip:
            index += 1
            continue
        return token
    return args[-1]


def _tail_stream(chunks: Any, limit: int) -> bytes:
    if limit < 1:
        return b""
    if isinstance(chunks, (bytes, str)):
        return _as_bytes(chunks)[-limit:]
    kept = bytearray()
    for chunk in chunks:
        piece = _as_bytes(chunk)
        if not piece:
            continue
        kept.extend(piece)
        if len(kept) > limit:
            del kept[:-limit]
    return bytes(kept)


def _as_bytes(value: bytes | str) -> bytes:
    if isinstance(value, bytes):
        return value
    return value.encode()


def _ok(stdout: str, stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], 0, stdout, stderr)


def _failed(stderr: str) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(["docker"], 1, "", stderr)
