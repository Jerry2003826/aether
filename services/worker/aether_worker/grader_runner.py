"""Container program for the grader.

The candidate never evaluates assertions. Protocol probes run in a dropped-privilege
child; expected values are not in this program. ``os.kill(-1, SIGKILL)`` runs only
as PID 1 inside the grader container's PID namespace.
"""

from __future__ import annotations

import json

PROTOCOL_CHILD_SOURCE = r"""
import importlib.util, json, os, sys
from pathlib import Path

def main():
    try:
        request = json.loads(sys.stdin.read())
        cases = request["cases"]
    except Exception:
        return
    result_fd = os.dup(1)
    null_fd = os.open(os.devnull, os.O_RDWR)
    os.dup2(null_fd, 0)
    os.dup2(null_fd, 1)
    os.dup2(null_fd, 2)
    os.close(null_fd)
    write = os.write
    leave = os._exit
    lookup = getattr
    dumps = json.dumps

    def blocked(code=0):
        raise RuntimeError("blocked")

    os._exit = blocked
    sys.exit = blocked
    try:
        import posix
        posix._exit = blocked
    except ImportError:
        pass

    def audit(event, args):
        if event.startswith(("subprocess.", "ctypes.", "os.system", "os.fork", "os.exec", "os.posix_spawn", "os.kill")):
            raise RuntimeError(event)

    sys.addaudithook(audit)
    root = Path("/work/src").resolve()
    sys.path.insert(0, str(root))
    loaded = {}
    values = []

    def candidate_path(rel):
        if not isinstance(rel, str):
            raise ValueError("path")
        parts = Path(rel).parts
        if not parts or rel.startswith(("/", "\\")) or ".." in parts:
            raise ValueError("path")
        raw = root.joinpath(*parts)
        if raw.is_symlink() or not raw.is_file():
            raise ValueError("path")
        path = raw.resolve()
        if not path.is_relative_to(root):
            raise ValueError("path")
        return path

    def encode(value):
        def convert(obj):
            if isinstance(obj, tuple):
                return list(obj)
            raise TypeError(type(obj).__name__)
        return dumps(value, default=convert, allow_nan=False, separators=(",", ":"))

    try:
        if not isinstance(cases, list):
            raise ValueError("cases")
        for case in cases:
            path = candidate_path(case["file"])
            key = str(path)
            module = loaded.get(key)
            if module is None:
                spec = importlib.util.spec_from_file_location(case["module"], path)
                if spec is None or spec.loader is None:
                    raise ImportError(case["module"])
                module = importlib.util.module_from_spec(spec)
                sys.modules[case["module"]] = module
                spec.loader.exec_module(module)
                loaded[key] = module
            target = module
            for name in case["attr"]:
                target = lookup(target, name)
            if case["kind"] == "call":
                if not isinstance(case["args"], list) or not isinstance(case["kwargs"], dict):
                    raise ValueError("args")
                target = target(*case["args"], **case["kwargs"])
            elif case["kind"] != "attr":
                raise ValueError("kind")
            values.append({"id": case["id"], "value": json.loads(encode(target))})
        payload = dumps({"values": values}, separators=(",", ":"), allow_nan=False)
    except Exception as exc:
        payload = dumps({"error": type(exc).__name__}, separators=(",", ":"))
    write(result_fd, payload.encode())
    leave(0)

main()
"""

_GRADER_TEMPLATE = r"""
import ast, json, os, signal, stat, subprocess, sys
import xml.etree.ElementTree as ElementTree
from pathlib import Path

NOBODY = 65534
REPORT_LIMIT = 4 * 1024 * 1024
VALUE_LIMIT = 1024 * 1024
SUITE = Path("/work/suite")
CHILD_HOME = Path("/run/aether/child")
PROTOCOL_CHILD = __PROTOCOL_CHILD__


def reap():
    # PID 1 inside the grader container. This signal stays in that PID namespace.
    try:
        os.kill(-1, signal.SIGKILL)
    except ProcessLookupError:
        pass


def required_tests(root):
    required = set()
    for path in sorted(root.rglob("*.py")):
        if not (path.name.startswith("test_") or path.name.endswith("_test.py")):
            continue
        module = ".".join(path.relative_to(root).with_suffix("").parts)
        try:
            tree = ast.parse(path.read_text(encoding="utf-8"))
        except (SyntaxError, UnicodeError, OSError):
            required.add((module, "<unparseable>"))
            continue
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith("test"):
                required.add((module, node.name))
            elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)) and item.name.startswith("test"):
                        required.add((module + "." + node.name, item.name))
    return required


def read_report(path):
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError:
        return None
    try:
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_size > REPORT_LIMIT:
            return None
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks)
    finally:
        os.close(fd)


def dropped(command, **kwargs):
    options = {
        "user": NOBODY,
        "group": NOBODY,
        "extra_groups": [],
        "stdout": kwargs.get("stdout", subprocess.DEVNULL),
        "stderr": kwargs.get("stderr", subprocess.DEVNULL),
        "cwd": kwargs.get("cwd"),
        "env": kwargs.get("env"),
        "text": kwargs.get("text", False),
        "timeout": kwargs.get("timeout", 300),
    }
    if kwargs.get("input") is None:
        options["stdin"] = kwargs.get("stdin", subprocess.DEVNULL)
    else:
        options["input"] = kwargs["input"]
    return subprocess.run(command, **options)


def child_env():
    return {
        "PATH": "/usr/local/bin:/usr/bin:/bin",
        "HOME": str(CHILD_HOME),
        "TMPDIR": str(CHILD_HOME),
        "LANG": "C.UTF-8",
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTEST_DISABLE_PLUGIN_AUTOLOAD": "1",
    }


def prepare_home():
    CHILD_HOME.mkdir(mode=0o700, exist_ok=True)
    os.chown(CHILD_HOME, NOBODY, NOBODY)


def suite_is_hidden():
    probe = (
        "import os, sys\n"
        "from pathlib import Path\n"
        "root = Path('/work/suite')\n"
        "if os.access(root, os.R_OK) or os.access(root, os.X_OK):\n"
        "    sys.exit(1)\n"
        "try:\n"
        "    files = [p for p in root.rglob('*') if p.is_file()]\n"
        "except OSError:\n"
        "    files = []\n"
        "sys.exit(1 if any(os.access(p, os.R_OK) for p in files) else 0)\n"
    )
    try:
        completed = dropped([sys.executable, "-I", "-c", probe], timeout=30)
    except subprocess.TimeoutExpired:
        return False
    return completed.returncode == 0


def has_expected_key(node):
    if isinstance(node, dict):
        if "expected" in node:
            return True
        return any(has_expected_key(value) for value in node.values())
    if isinstance(node, list):
        return any(has_expected_key(value) for value in node)
    return False


def run_protocol():
    try:
        request = json.loads(Path("/work/protocol.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError):
        return {"ok": False, "trust": "isolated", "reason": "protocol_unreadable", "exit_code": None, "tests": 0, "values": None, "cooperative_passed": False}
    if not isinstance(request, dict) or has_expected_key(request) or not isinstance(request.get("cases"), list):
        return {"ok": False, "trust": "isolated", "reason": "protocol_invalid", "exit_code": None, "tests": 0, "values": None, "cooperative_passed": False}
    if not suite_is_hidden():
        return {"ok": False, "trust": "isolated", "reason": "suite_exposed", "exit_code": None, "tests": 0, "values": None, "cooperative_passed": False}
    prepare_home()
    try:
        completed = dropped(
            [sys.executable, "-I", "-c", PROTOCOL_CHILD],
            input=json.dumps(request),
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            cwd="/work/src",
            env=child_env(),
        )
        exit_code = completed.returncode
        output = completed.stdout
    except subprocess.TimeoutExpired:
        exit_code = None
        output = ""
    reap()
    if exit_code != 0 or len(output) > VALUE_LIMIT:
        return {"ok": False, "trust": "isolated", "reason": "candidate_failed", "exit_code": exit_code, "tests": 0, "values": None, "cooperative_passed": False}
    try:
        body = json.loads(output)
    except ValueError:
        return {"ok": False, "trust": "isolated", "reason": "candidate_unreadable", "exit_code": exit_code, "tests": 0, "values": None, "cooperative_passed": False}
    if not isinstance(body, dict) or not isinstance(body.get("values"), list):
        return {"ok": False, "trust": "isolated", "reason": "candidate_unreadable", "exit_code": exit_code, "tests": 0, "values": None, "cooperative_passed": False}
    return {"ok": True, "trust": "isolated", "reason": "values", "exit_code": 0, "tests": len(body["values"]), "values": body["values"], "cooperative_passed": False}


def grade_pytest():
    required = required_tests(SUITE)
    if not required:
        return {"ok": False, "reason": "suite_has_no_tests", "exit_code": None, "tests": 0}
    prepare_home()
    report = CHILD_HOME / (os.urandom(16).hex() + ".xml")
    command = [
        sys.executable, "-I", "-m", "pytest", "-q", "--tb=no", "--noconftest", "-p", "no:cacheprovider",
        "-o", "pythonpath=/work/src", "--rootdir", str(SUITE), "--junitxml", str(report), str(SUITE),
    ]
    try:
        completed = dropped(command, cwd=str(SUITE), env=child_env())
        exit_code = completed.returncode
    except subprocess.TimeoutExpired:
        exit_code = None
    reap()
    if exit_code != 0:
        return {"ok": False, "reason": "pytest_failed", "exit_code": exit_code, "tests": 0}
    data = read_report(report)
    if data is None:
        return {"ok": False, "reason": "report_missing", "exit_code": exit_code, "tests": 0}
    try:
        root = ElementTree.fromstring(data)
    except ElementTree.ParseError:
        return {"ok": False, "reason": "report_unreadable", "exit_code": exit_code, "tests": 0}
    total = 0
    passed = set()
    for case in root.iter("testcase"):
        total += 1
        key = (case.get("classname", ""), case.get("name", "").split("[", 1)[0])
        if any(child.tag in {"failure", "error", "skipped"} for child in case):
            return {"ok": False, "reason": "test_not_passed", "exit_code": exit_code, "tests": total}
        passed.add(key)
    if total < 1 or not required <= passed:
        return {"ok": False, "reason": "required_test_missing", "exit_code": exit_code, "tests": total}
    return {"ok": True, "reason": "passed", "exit_code": exit_code, "tests": total}


def finish_pytest(result):
    passed = result.get("ok") is True
    result["ok"] = False
    result["trust"] = "cooperative"
    result["values"] = None
    result["cooperative_passed"] = passed
    result["security_certified"] = False
    if passed:
        result["reason"] = "cooperative_only"
    return result


def run():
    if Path("/work/protocol.json").is_file():
        return run_protocol()
    return finish_pytest(grade_pytest())


sentinel = Path(os.environ.get("AETHER_HOST_SENTINEL", "/aether-host-sentinel"))
keys = ("AETHER_PROVIDER_KEY", "AETHER_ADMIN_DATABASE_URL")
isolation = {"sentinel_visible": sentinel.exists(), "credential_present": any(key in os.environ for key in keys)}
sys.stdout.write("ISOLATION " + json.dumps(isolation) + "\n")
sys.stdout.flush()
sys.stdout.write("GRADE " + json.dumps(run()) + "\n")
sys.stdout.flush()
"""

GRADER_PROGRAM = _GRADER_TEMPLATE.replace("__PROTOCOL_CHILD__", json.dumps(PROTOCOL_CHILD_SOURCE))
