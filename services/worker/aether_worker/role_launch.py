"""Hand privileged credentials to separate processes, then drop them from the worker."""

from __future__ import annotations

import json
import os
import queue
import secrets
import signal
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlsplit

import psutil

from aether_worker.loopback import LoopbackError, get_json
from aether_worker.startup_budget import IMAGE_READY_STEP, PHASE, ROLE_HEALTH_SECONDS, ROLE_READY_SECONDS

_PROCESS_ROLE = "AETHER_PROCESS_ROLE"
_LOOPBACK_TOKEN = "AETHER_LOOPBACK_TOKEN"
_VERIFIER_DATABASE_URL = "AETHER_VERIFIER_DATABASE_URL"
_INDEX_ROOT = "AETHER_INDEX_ROOT"
_DEFAULT_INDEX = "data/indexes"
_WORKER_ROLE = "worker"
_VERIFIER_ROLE = "verifier"
_ENCODING = "utf-8"
PRIVILEGED_ENV = (_VERIFIER_DATABASE_URL, "AETHER_LIVE_MODEL_KEY")
_READY_TIMEOUT = ROLE_READY_SECONDS
# A first OpenHands image build on a clean host. It runs before any role starts, so it is
# not part of the 60 s readiness wait.
_PREPARE_TIMEOUT = 3600.0
_HEALTH_CALL_SECONDS = 10
_ROLE_COMMANDS = ("aether_worker.verifier_process", "aether_gateway", "aether_worker.runner_process")


def reject_privileged_worker_env() -> None:
    """A process that is already the worker must not still hold verifier or provider secrets."""
    if os.environ.get(_PROCESS_ROLE) != _WORKER_ROLE:
        return
    present = [name for name in PRIVILEGED_ENV if os.environ.get(name)]
    if present:
        raise SystemExit("worker 进程不能持有: " + ", ".join(present))


def scrub_privileged_env() -> None:
    for name in PRIVILEGED_ENV:
        os.environ.pop(name, None)
    os.environ[_PROCESS_ROLE] = _WORKER_ROLE


@dataclass(frozen=True)
class RolePorts:
    verifier_base: str
    gateway_base: str
    runner_base: str
    loopback_token: str


def stop_children(children: list[subprocess.Popen[str]]) -> None:
    for proc in children:
        if proc.poll() is None:
            proc.terminate()
    for proc in children:
        if proc.poll() is None:
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


def _phase(step: str) -> None:
    print(f"{PHASE} step={step}", flush=True)


def launch_privileged_roles(
    role_hook: Callable[[str], None] | None = None,
) -> tuple[RolePorts, list[subprocess.Popen[str]]]:
    """role_hook(role) runs before each role is awaited; only tests pass it (never read from the environment)."""
    missing = [
        name
        for name in ("AETHER_WORKER_DATABASE_URL", _VERIFIER_DATABASE_URL, "AETHER_DISPATCHER_DATABASE_URL", "AETHER_TEMPORAL_ADDRESS")
        if not os.environ.get(name)
    ]
    if missing:
        raise SystemExit("worker 缺少环境变量: " + ", ".join(missing))
    stop_recorded_roles()
    verifier_url = os.environ[_VERIFIER_DATABASE_URL]
    provider_key = os.environ.get("AETHER_LIVE_MODEL_KEY", "")
    token = secrets.token_urlsafe(32)
    secrets_to_hide = [verifier_url, provider_key, token, _password(verifier_url)]
    _prepare_runner_image(secrets_to_hide)
    _phase(IMAGE_READY_STEP)
    # No provider key, no gateway: an empty AETHER_GATEWAY_BASE is how the worker learns that live
    # research has no model (_missing_live_settings -> live_provider_unconfigured, before any
    # reservation). A keyless gateway would admit research and turn the provider's 401 into a sent call.
    start_gateway = bool(provider_key.strip())
    if not start_gateway:
        sys.stderr.write("AETHER_LIVE_MODEL_KEY 未配置：不启动模型网关，live 研究以 live_provider_unconfigured 进入待审，配置后重启 worker 再恢复\n")
    children: list[subprocess.Popen[str]] = []
    try:
        verifier_proc, verifier_errors = _open(
            "aether_worker.verifier_process",
            _verifier_env(token),
            secrets_to_hide,
            {"verifier_url": verifier_url},
        )
        children.append(verifier_proc)
        gateway: tuple[subprocess.Popen[str], list[str]] | None = None
        if start_gateway:
            gateway = _open(
                "aether_gateway",
                _gateway_env(token),
                secrets_to_hide,
                {"provider_key": provider_key, "worker_url": os.environ["AETHER_WORKER_DATABASE_URL"]},
            )
            children.append(gateway[0])
        runner_proc, runner_errors = _open("aether_worker.runner_process", _runner_env(token), secrets_to_hide, None)
        children.append(runner_proc)
        hook = role_hook or (lambda _role: None)
        hook(_VERIFIER_ROLE)
        verifier_base = _await_role(verifier_proc, verifier_errors, secrets_to_hide)
        _phase("role-ready role=verifier")
        gateway_base = ""
        if gateway is not None:
            hook("gateway")
            gateway_base = _await_role(gateway[0], gateway[1], secrets_to_hide)
            _phase("role-ready role=gateway")
        hook("runner")
        runner_base = _await_role(runner_proc, runner_errors, secrets_to_hide)
        _phase("role-ready role=runner")
        scrub_privileged_env()
        os.environ["AETHER_VERIFIER_BASE"] = verifier_base
        os.environ["AETHER_GATEWAY_BASE"] = gateway_base
        os.environ["AETHER_RUNNER_BASE"] = runner_base
        os.environ[_LOOPBACK_TOKEN] = token
        _record_role_pids([proc.pid for proc in children])
        ports = RolePorts(verifier_base=verifier_base, gateway_base=gateway_base, runner_base=runner_base, loopback_token=token)
        _require_health(verifier_base, _VERIFIER_ROLE, secrets_to_hide)
        if gateway_base:
            _require_health(gateway_base, "gateway", secrets_to_hide)
        _require_health(runner_base, "runner", secrets_to_hide)
        _phase("roles-ready")
        _write_boundary(ports)
        _close_pipes(children)
        return ports, children
    except BaseException:
        scrub_privileged_env()
        stop_children(children)
        raise


def _base_env() -> dict[str, str]:
    kept = ("PATH", "HOME", "LANG", "LC_ALL", "VIRTUAL_ENV", "PYTHONPATH", "PYTHONHOME", "SSL_CERT_FILE", "SSL_CERT_DIR", "TMPDIR")
    return {key: os.environ[key] for key in kept if os.environ.get(key)}


def _verifier_env(token: str) -> dict[str, str]:
    env = _base_env()
    env.update(
        {
            _PROCESS_ROLE: _VERIFIER_ROLE,
            _LOOPBACK_TOKEN: token,
            "AETHER_ARTIFACT_ROOT": os.environ.get("AETHER_ARTIFACT_ROOT", "data/artifacts"),
            "AETHER_RESEARCH_PROVIDER": os.environ.get("AETHER_RESEARCH_PROVIDER", "live"),
        }
    )
    return env


def _gateway_env(token: str) -> dict[str, str]:
    env = _base_env()
    env.update(
        {
            _PROCESS_ROLE: "gateway",
            "AETHER_LIVE_MODEL_BASE": os.environ.get("AETHER_LIVE_MODEL_BASE", ""),
            _LOOPBACK_TOKEN: token,
        }
    )
    alias = os.environ.get("AETHER_LIVE_MODEL_ALIAS", "").strip()
    if alias:
        env["AETHER_LIVE_MODEL_ALIAS"] = alias
    return env


# What the runner labels coding containers with and what its startup sweep selects by
# (coding_sandbox.worker_instance_id / deployment_label): the worker's own values, so both match it.
_RUNNER_IDENTITY_ENV = ("AETHER_WORKER_INSTANCE_ID", "AETHER_WORKER_STATE_DIR", "AETHER_DEPLOYMENT_ID")


def _runner_env(token: str) -> dict[str, str]:
    env = _base_env()
    env.update({name: os.environ[name] for name in _RUNNER_IDENTITY_ENV if os.environ.get(name)})
    env[_PROCESS_ROLE] = "runner"
    env[_LOOPBACK_TOKEN] = token
    return env


def _prepare_runner_image(secrets_to_hide: list[str]) -> None:
    """Ensure the OpenHands image in a one-shot runner-role process before any role starts.

    Docker stays out of the worker process. A failure or timeout is reported and the roles
    still start; a coding job then fails as image_unavailable instead of the runner timing out.
    """
    env = _base_env()
    env[_PROCESS_ROLE] = "runner"
    try:
        completed = subprocess.run(
            [sys.executable, "-m", "aether_worker.runner_process", "--prepare-image"],
            env=env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=_PREPARE_TIMEOUT,
            check=False,
        )
    except subprocess.TimeoutExpired:
        sys.stderr.write("runner image prepare timed out\n")
        return
    if completed.returncode != 0:
        sys.stderr.write(_redact(f"runner image prepare failed {completed.returncode}: {(completed.stderr or '')[-800:]}", secrets_to_hide) + "\n")


def _open(
    module: str,
    env: dict[str, str],
    secrets_to_hide: list[str],
    payload: dict[str, str] | None,
) -> tuple[subprocess.Popen[str], list[str]]:
    proc = subprocess.Popen(
        [sys.executable, "-m", module],
        env=env,
        stdin=subprocess.PIPE if payload is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stderr is not None
    if payload is not None:
        assert proc.stdin is not None
        proc.stdin.write(json.dumps(payload) + "\n")
        proc.stdin.close()
    errors: list[str] = []
    threading.Thread(target=_drain, args=(proc.stderr, errors, secrets_to_hide), daemon=True).start()
    return proc, errors


def _await_role(proc: subprocess.Popen[str], errors: list[str], secrets_to_hide: list[str]) -> str:
    try:
        line = _read_ready(proc, secrets_to_hide, errors)
    except BaseException:
        if proc.poll() is None:
            proc.terminate()
        raise
    return f"http://127.0.0.1:{_port(line)}"


def _discard_lines(lines: queue.Queue[str]) -> None:
    while True:
        lines.get()


def _read_ready(proc: subprocess.Popen[str], secrets_to_hide: list[str], errors: list[str]) -> str:
    """Wait until the role prints its ready line, on time.monotonic().

    readline runs on a side thread. A partial line blocks that call, and the deadline is checked
    without waiting for it to return, so the wait cannot become unbounded.
    """
    assert proc.stdout is not None
    stdout = proc.stdout
    deadline = time.monotonic() + _READY_TIMEOUT
    lines: queue.Queue[str] = queue.Queue()

    def _pump() -> None:
        try:
            while True:
                line = stdout.readline()
                lines.put(line)
                if line == "":
                    return
        except (OSError, ValueError):
            lines.put("")

    threading.Thread(target=_pump, name="aether-role-stdout", daemon=True).start()
    while True:
        if time.monotonic() >= deadline:
            raise SystemExit(_redact("角色进程没有就绪: " + "".join(errors)[-800:], secrets_to_hide))
        if proc.poll() is not None and lines.empty():
            raise SystemExit(_redact(f"角色进程退出 {proc.returncode}: {''.join(errors)[-800:]}", secrets_to_hide))
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise SystemExit(_redact("角色进程没有就绪: " + "".join(errors)[-800:], secrets_to_hide))
        try:
            line = lines.get(timeout=min(0.05, remaining))
        except queue.Empty:
            continue
        if line.startswith("aether-role-ready "):
            threading.Thread(target=_discard_lines, args=(lines,), name="aether-role-stdout-discard", daemon=True).start()
            return line.strip()
        if line == "" and proc.poll() is not None:
            raise SystemExit(_redact(f"角色进程退出 {proc.returncode}: {''.join(errors)[-800:]}", secrets_to_hide))


def _drain(pipe, bucket: list[str], secrets_to_hide: list[str]) -> None:
    for line in pipe:
        bucket.append(_redact(line, secrets_to_hide))


def _port(line: str) -> int:
    for part in line.split():
        if part.startswith("port="):
            return int(part.split("=", 1)[1])
    raise SystemExit("角色没有报告端口")


def _require_health(base: str, role: str, secrets_to_hide: list[str]) -> None:
    deadline = time.monotonic() + ROLE_HEALTH_SECONDS
    last = "unreachable"
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            payload = get_json(f"{base}/health", timeout=min(_HEALTH_CALL_SECONDS, remaining))
        except LoopbackError as exc:
            last = exc.code
            pause = min(0.05, max(0.0, deadline - time.monotonic()))
            if pause:
                time.sleep(pause)
            continue
        if payload.get("role") != role:
            raise SystemExit(f"{role} 健康检查角色不符")
        if role == _VERIFIER_ROLE and payload.get("db_user") != "aether_verifier":
            raise SystemExit("verifier 数据库角色不符")
        return
    raise SystemExit(_redact(f"{role} 健康检查失败: {last}", secrets_to_hide))


def _close_pipes(children: list[subprocess.Popen[str]]) -> None:
    for proc in children:
        for pipe in (proc.stdin, proc.stdout, proc.stderr):
            if pipe is not None and not pipe.closed:
                pipe.close()


def _index_root() -> Path:
    return Path(os.environ.get(_INDEX_ROOT, _DEFAULT_INDEX))


def _replace_json(path: Path, payload: str) -> None:
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(payload, encoding=_ENCODING)
    temporary.replace(path)


def _write_boundary(ports: RolePorts) -> None:
    root = _index_root()
    root.mkdir(parents=True, exist_ok=True)
    payload = {
        "process_role": _WORKER_ROLE,
        "holds_verifier_database_url": False,
        "holds_live_model_key": False,
        "holds_docker_socket": False,
        "verifier_base": ports.verifier_base,
        "gateway_base": ports.gateway_base,
        "runner_base": ports.runner_base,
    }
    _replace_json(root / "boundary.json", json.dumps(payload, ensure_ascii=False, sort_keys=True))


def _record_role_pids(pids: list[int]) -> None:
    stop_recorded_roles(except_pids=set(pids))
    root = _index_root()
    root.mkdir(parents=True, exist_ok=True)
    _replace_json(root / "role-pids.json", json.dumps(pids))


def stop_recorded_roles(except_pids: set[int] | None = None) -> None:
    path = _index_root() / "role-pids.json"
    if not path.is_file():
        return
    try:
        recorded = json.loads(path.read_text(encoding=_ENCODING))
    except json.JSONDecodeError:
        return
    if not isinstance(recorded, list):
        return
    skip = except_pids or set()
    for item in recorded:
        pid = _role_pid(item, skip)
        if pid is None or not _is_recorded_role(pid):
            continue
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            continue


def _role_pid(item: object, skip: set[int]) -> int | None:
    if isinstance(item, bool) or not isinstance(item, (int, str)):
        return None
    try:
        pid = int(item)
    except ValueError:
        return None
    if pid <= 1 or pid == os.getpid() or pid in skip:
        return None
    return pid


def _is_recorded_role(pid: int) -> bool:
    try:
        text = " ".join(psutil.Process(pid).cmdline())
    except (psutil.Error, OSError):
        return False
    return any(name in text for name in _ROLE_COMMANDS)


def _password(url: str) -> str:
    parsed = urlsplit(url)
    return parsed.password or ""


def _redact(text: str, secrets_to_hide: list[str]) -> str:
    redacted = text
    for secret in secrets_to_hide:
        if secret:
            redacted = redacted.replace(secret, "[REDACTED]")
    return redacted
