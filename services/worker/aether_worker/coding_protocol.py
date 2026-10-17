"""Stdlib test protocol executed by OpenHands inside the sandbox. It does not score or import OpenHands.

Integrity: before any candidate code runs, every Python source and grading file under the workspace
(virtualenvs with pyvenv.cfg, node_modules, VCS and cache dirs skipped) is read into memory and hashed. All Python imports from the workspace (test modules, oracle.py, candidate code, packages)
are served from that in-memory snapshot, so a file rewritten during the run has no effect on any
module loaded afterwards. Those files are hashed again before each test and after the run;
one that is missing or changed fails the suite with `grading_tampered`. New files are fine.

Isolation: three kinds of process, and candidate code only ever runs in the last one.
  grader     this process. Reads (and deletes) the nonce file first, takes the snapshot, reads the
             test names from the baseline source, and aggregates verdicts. It never imports or runs
             workspace code. Its one stdout line, signed with the nonce, is the result.
  test       one fresh process per test (plus one per module to import it and find its tests). It
             runs the trusted test module and oracle from the snapshot and replies on a pipe only it
             holds. A test without a well-formed reply (crash, early exit, timeout) is a FAIL.
  candidate  started by the test process. Candidate modules there are proxies: attribute access and
             calls cross a pipe and only data comes back (builtin values, or opaque handles that refuse
             comparison and truth), so candidate code can neither reach the test's frames, oracle or
             result, nor hand back an object that compares equal to everything.
The grader and test processes are made non-dumpable (best effort, Linux), which keeps same-uid
processes out of their /proc fd/mem/environ and ptrace.
"""

from __future__ import annotations

import ast
import base64
import builtins
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
import select
import struct
import subprocess
import sys
import time
import types
from pathlib import Path
from typing import Any

# grading_files.py ships next to this script (repo and image); `-I` drops the script dir from sys.path.
# Appended, then removed, so it cannot shadow any other module.
_HERE = str(Path(__file__).resolve().parent)
sys.path.append(_HERE)
try:
    import grading_files
finally:
    sys.path.remove(_HERE)

# Never walked: VCS data, caches, virtualenvs and vendored dependency trees.
_SKIP_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".hg",
        ".svn",
        ".aether-state",
        ".pytest_cache",
        ".mypy_cache",
        ".ruff_cache",
        ".tox",
        ".nox",
        "node_modules",
        "site-packages",
    }
)
# Skipped only when they really are virtualenvs (pyvenv.cfg); otherwise they may be candidate packages.
_VENV_DIRS = frozenset({".venv", "venv", "env"})


def _skipped(path: Path) -> bool:
    if path.name in _SKIP_DIRS:
        return True
    return path.name in _VENV_DIRS and (path / "pyvenv.cfg").is_file()


def _guarded(name: str) -> bool:
    """Snapshot and verify Python sources and grading files; other data may change freely."""
    return name.endswith(".py") or grading_files.is_grading_path(name)


def _files(root: Path) -> dict[str, Path]:
    found: dict[str, Path] = {}
    pending = [root]
    while pending:
        current = pending.pop()
        try:
            entries = sorted(current.iterdir())
        except OSError:
            continue
        for path in entries:
            if path.is_symlink():
                continue
            if path.is_dir():
                if not _skipped(path):
                    pending.append(path)
                continue
            name = path.relative_to(root).as_posix()
            if path.is_file() and _guarded(name):
                found[name] = path
    return found


def _tampered(root: Path, baseline: dict[str, str]) -> bool:
    """A snapshotted file that is now missing or different. New files are fine."""
    for name, digest in baseline.items():
        try:
            data = (root / name).read_bytes()
        except OSError:
            return True
        if (root / name).is_symlink() or hashlib.sha256(data).hexdigest() != digest:
            return True
    return False


class _SnapshotLoader(importlib.abc.Loader):
    def __init__(self, source: bytes, origin: str) -> None:
        self._source = source
        self._origin = origin

    def create_module(self, spec):
        return None

    def exec_module(self, module) -> None:
        code = compile(self._source, self._origin, "exec", dont_inherit=True)
        exec(code, module.__dict__)  # noqa: S102 - running the suite is the point


def _module_map(sources: dict[str, bytes], bases: list[str]) -> dict[str, tuple[str, bool]]:
    """Import name -> (relative path, is package). The test's own non-package directory first (so a test
    there imports its sibling helper by bare name, as with pytest's default prepend mode), then the root.

    Each test process gets only its own test's directory, so same-named helpers in two test
    directories never resolve to each other, and a module added at the root cannot shadow a helper.
    """
    found: dict[str, tuple[str, bool]] = {}
    for base in [*bases, ""]:
        prefix = base + "/" if base else ""
        for name in sources:
            if not name.endswith(".py") or not name.startswith(prefix):
                continue
            parts = name[len(prefix) : -3].split("/")
            package = parts[-1] == "__init__"
            if package:
                parts = parts[:-1]
            if not parts or not all(part.isidentifier() for part in parts):
                continue
            found.setdefault(".".join(parts), (name, package))
    return found


def _spec(fullname: str, origin_path: Path, data: bytes, package: bool = False):
    origin = str(origin_path)
    spec = importlib.machinery.ModuleSpec(fullname, _SnapshotLoader(data, origin), origin=origin, is_package=package)
    if package:
        spec.submodule_search_locations = [str(origin_path.parent)]
    spec.has_location = True
    return spec


def _is_test_module(name: str) -> bool:
    base = name.rsplit("/", 1)[-1]
    return base.endswith(".py") and (base.startswith("test_") or base.endswith("_test.py"))


def _is_candidate(name: str) -> bool:
    """Workspace Python that is not grading: the candidate's code. It only ever runs in the candidate process."""
    return name.endswith(".py") and not grading_files.is_grading_path(name)


def _test_modules(sources: dict[str, bytes]) -> list[tuple[str, str, str]]:
    """(relative path, import name or "" for a direct load, base dir) of every test module, root first."""
    found: list[tuple[str, str, str]] = []
    for name in sources:
        if not _is_test_module(name) or _is_candidate(name):
            continue
        parts = name[:-3].split("/")
        if not all(part.isidentifier() for part in parts):
            continue
        # Climb while the parent directory is a package; the first non-package directory is the base.
        depth = len(parts) - 1
        while depth > 0 and "/".join(parts[:depth]) + "/__init__.py" in sources:
            depth -= 1
        base = "/".join(parts[:depth])
        # A package under a non-package base is imported through the finder (relative imports work);
        # a plain module there is loaded directly under its full dotted path.
        importable = depth == 0 or depth < len(parts) - 1
        found.append((name, ".".join(parts[depth:]) if importable else "", base))
    return sorted(found, key=lambda item: (item[0].count("/"), item[0]))


# ---------------------------------------------------------------------------------------------
# The channel between the trusted grader and the candidate process: length-prefixed JSON on a
# dedicated pipe pair. Only data crosses it; the grader never unpickles or executes anything it reads.

_MAX_MESSAGE = 32 * 1024 * 1024
_MAX_DEPTH = 100
_CALL_TIMEOUT_ENV = "AETHER_PROTOCOL_CALL_TIMEOUT_SECONDS"
_NONCE_ENV = "AETHER_PROTOCOL_NONCE_FILE"
_TEST_TIMEOUT_ENV = "AETHER_PROTOCOL_TEST_TIMEOUT_SECONDS"


class _ChannelClosed(Exception):
    pass


def _send(fd: int, message: object) -> None:
    data = json.dumps(message, separators=(",", ":"), allow_nan=False).encode("utf-8")
    if len(data) > _MAX_MESSAGE:
        raise ValueError("message_too_large")
    view = memoryview(struct.pack(">I", len(data)) + data)
    while view:
        written = os.write(fd, view)
        view = view[written:]


def _read_exact(fd: int, size: int, deadline: float | None) -> bytes:
    chunks: list[bytes] = []
    while size:
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0 or not select.select([fd], [], [], remaining)[0]:
                raise TimeoutError("candidate_timeout")
        block = os.read(fd, min(size, 1 << 20))
        if not block:
            raise _ChannelClosed("candidate_exited")
        chunks.append(block)
        size -= len(block)
    return b"".join(chunks)


def _receive(fd: int, deadline: float | None = None) -> object:
    (size,) = struct.unpack(">I", _read_exact(fd, 4, deadline))
    if size > _MAX_MESSAGE:
        raise _ChannelClosed("message_too_large")
    return json.loads(_read_exact(fd, size, deadline))


def _float_text(value: float) -> str:
    return repr(float(value))


# ---------------------------------------------------------------------------------------------
# Trusted side.


class CandidateError(Exception):
    """Candidate code raised an exception with no builtin equivalent, or the candidate process failed."""


class _Session:
    """One candidate process. Started lazily; a new one for every test process (so for every test)."""

    def __init__(self, root: Path, sources: dict[str, bytes], bases: list[str], timeout: float) -> None:
        self._root = root
        self._sources = sources
        self._bases = bases
        self._timeout = timeout
        self._process: subprocess.Popen[bytes] | None = None
        self._to: int | None = None
        self._from: int | None = None
        self._dead: str | None = None
        self._modules: dict[str, int] = {}

    def _start(self) -> None:
        child_in, to_child = os.pipe()
        from_child, child_out = os.pipe()
        env = {key: value for key, value in os.environ.items() if not key.startswith("AETHER_PROTOCOL_")}
        try:
            self._process = subprocess.Popen(  # this interpreter running this file
                [sys.executable, "-I", str(Path(__file__).resolve()), "--candidate", str(child_in), str(child_out)],
                cwd=self._root,
                env=env,
                stdin=subprocess.DEVNULL,
                # Candidate output never reaches the grader's stdout, where the signed result goes.
                stdout=sys.stderr.fileno(),
                stderr=sys.stderr.fileno(),
                pass_fds=(child_in, child_out),
                # Stays in the test process's group, which the grader kills after every test.
            )
        finally:
            os.close(child_in)
            os.close(child_out)
        self._to, self._from = to_child, from_child
        candidates = {name: base64.b64encode(data).decode("ascii") for name, data in self._sources.items() if _is_candidate(name)}
        self._request({"op": "init", "sources": candidates, "bases": self._bases, "all": sorted(self._sources)})

    def _request(self, message: dict[str, object]) -> object:
        if self._dead is not None:
            raise CandidateError(self._dead)
        if self._process is None:
            self._start()
        assert self._to is not None and self._from is not None
        try:
            _send(self._to, message)
            reply = _receive(self._from, time.monotonic() + self._timeout)
        except (TimeoutError, _ChannelClosed, OSError, ValueError) as exc:
            self._dead = str(exc) or type(exc).__name__
            self.close()
            raise CandidateError(self._dead) from None
        if not isinstance(reply, dict):
            self._dead = "candidate_protocol"
            self.close()
            raise CandidateError(self._dead)
        if "err" in reply:
            raise _exception(reply["err"])
        return _decode(reply.get("ok"), self)

    def call(self, op: str, **fields: object) -> object:
        return self._request({"op": op, **fields})

    def module_handle(self, name: str) -> int:
        if name not in self._modules:
            value = self.call("import", name=name)
            if not isinstance(value, _Remote):
                raise CandidateError("candidate_protocol")
            self._modules[name] = value._aether_id
        return self._modules[name]

    def close(self) -> None:
        for fd in (self._to, self._from):
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
        self._to = self._from = None
        process, self._process = self._process, None
        if process is not None:
            try:
                process.kill()
            except OSError:
                pass
            process.wait()
        if self._dead is None:
            self._dead = "candidate_closed"


class _Current:
    session: _Session | None = None


def _session() -> _Session:
    if _Current.session is None:
        raise CandidateError("candidate_unavailable")
    return _Current.session


_BUILTIN_ERRORS = {
    name: value
    for name, value in vars(builtins).items()
    if isinstance(value, type) and issubclass(value, Exception) and not name.startswith("_")
}


def _exception(raw: object) -> Exception:
    """A trusted exception for a candidate one. Only builtin exception types are recreated."""
    if not (isinstance(raw, list) and len(raw) == 3 and all(type(item) is str for item in raw)):
        return CandidateError("candidate_protocol")
    base, qualname, message = raw
    kind = _BUILTIN_ERRORS.get(base, CandidateError)
    text = message[:2000]
    try:
        made = kind(text)
    except Exception:  # noqa: BLE001 - an exception type with a special constructor
        made = CandidateError(f"{qualname}: {text}")
    made.candidate_type = qualname[:200]  # type: ignore[attr-defined]
    return made


def _decode(node: object, session: _Session, depth: int = 0) -> object:
    """Build only builtin values (and opaque handles) from what the candidate sent."""
    if depth > _MAX_DEPTH or not isinstance(node, list) or not node or type(node[0]) is not str:
        raise CandidateError("candidate_protocol")
    tag, rest = node[0], node[1:]
    if tag == "n" and not rest:
        return None
    if len(rest) != 1 and tag != "h":
        raise CandidateError("candidate_protocol")
    value = rest[0] if rest else None
    if tag == "b" and type(value) is bool:
        return value
    if tag == "i" and type(value) is str:
        return int(value)
    if tag == "f" and type(value) is str:
        return float(value)
    if tag == "c" and isinstance(value, list) and len(value) == 2 and all(type(item) is str for item in value):
        return complex(float(value[0]), float(value[1]))
    if tag == "s" and type(value) is str:
        return value
    if tag == "y" and type(value) is str:
        return base64.b64decode(value, validate=True)
    if tag in {"l", "t", "S", "F"} and isinstance(value, list):
        items = [_decode(item, session, depth + 1) for item in value]
        return {"l": list, "t": tuple, "S": set, "F": frozenset}[tag](items)
    if tag == "d" and isinstance(value, list):
        result: dict[object, object] = {}
        for pair in value:
            if not (isinstance(pair, list) and len(pair) == 2):
                raise CandidateError("candidate_protocol")
            result[_decode(pair[0], session, depth + 1)] = _decode(pair[1], session, depth + 1)
        return result
    if tag == "h" and len(rest) == 2 and type(rest[0]) is int and type(rest[1]) is str:
        return _Remote(session, rest[0], rest[1][:200])
    raise CandidateError("candidate_protocol")


def _encode_trusted(value: object, depth: int = 0) -> list[object]:
    """Encode an argument the grader passes to candidate code. Only data and candidate handles."""
    if depth > _MAX_DEPTH:
        raise TypeError("argument_too_deep")
    if isinstance(value, _Remote):
        if value._aether_session is not _Current.session:
            raise CandidateError("candidate_restarted")
        return ["h", value._aether_id, ""]
    if isinstance(value, _RemoteModule):
        return ["h", _session().module_handle(value.__name__), ""]
    return _encode_data(value, depth, lambda item, level: _encode_trusted(item, level))


def _encode_data(value: object, depth: int, nested) -> list[object]:
    if value is None:
        return ["n"]
    if isinstance(value, bool):
        return ["b", bool(value)]
    if isinstance(value, int):
        return ["i", str(int(value))]
    if isinstance(value, float):
        return ["f", _float_text(value)]
    if isinstance(value, complex):
        return ["c", [_float_text(value.real), _float_text(value.imag)]]
    if isinstance(value, str):
        return ["s", str(value)]
    if isinstance(value, (bytes, bytearray)):
        return ["y", base64.b64encode(bytes(value)).decode("ascii")]
    if isinstance(value, (list, tuple, set, frozenset)):
        tag = "l" if isinstance(value, list) else "t" if isinstance(value, tuple) else "F" if isinstance(value, frozenset) else "S"
        return [tag, [nested(item, depth + 1) for item in value]]
    if isinstance(value, dict):
        return ["d", [[nested(key, depth + 1), nested(item, depth + 1)] for key, item in value.items()]]
    return []


def _encode_argument(value: object) -> list[object]:
    encoded = _encode_trusted(value)
    if not encoded:
        raise TypeError(f"cannot pass a {type(value).__name__} to candidate code; pass data")
    return encoded


def _encode_tree(value: object, depth: int = 0) -> list[object]:
    encoded = _encode_trusted(value, depth)
    if not encoded:
        raise TypeError(f"cannot pass a {type(value).__name__} to candidate code; pass data")
    return encoded


class _Remote:
    """A candidate object the grader can use but never trust.

    Attribute access, calls, items, len, iteration and str are answered by the candidate process
    and come back as data. Comparison and truth are refused, so a verdict can only rest on data.
    """

    __slots__ = ("_aether_id", "_aether_session", "_aether_type")

    def __init__(self, session: _Session, handle: int, kind: str) -> None:
        object.__setattr__(self, "_aether_session", session)
        object.__setattr__(self, "_aether_id", handle)
        object.__setattr__(self, "_aether_type", kind)

    def _aether(self, op: str, **fields: object) -> object:
        if self._aether_session is not _Current.session:
            raise CandidateError("candidate_restarted")
        return self._aether_session.call(op, target=self._aether_id, **fields)

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        return self._aether("getattr", name=name)

    def __setattr__(self, name: str, value: object) -> None:
        self._aether("setattr", name=name, value=_encode_tree(value))

    def __delattr__(self, name: str) -> None:
        self._aether("delattr", name=name)

    def __call__(self, *args: object, **kwargs: object) -> object:
        return self._aether(
            "call",
            args=[_encode_tree(item) for item in args],
            kwargs=[[str(key), _encode_tree(item)] for key, item in kwargs.items()],
        )

    def __getitem__(self, key: object) -> object:
        return self._aether("getitem", key=_encode_tree(key))

    def __setitem__(self, key: object, value: object) -> None:
        self._aether("setitem", key=_encode_tree(key), value=_encode_tree(value))

    def __len__(self) -> int:
        value = self._aether("len")
        if type(value) is not int:
            raise TypeError("len() of a candidate object is not an int")
        return value

    def __iter__(self):
        value = self._aether("list")
        if type(value) is not list:
            raise TypeError("candidate iteration did not produce a list")
        return iter(value)

    def __contains__(self, item: object) -> bool:
        value = self._aether("contains", item=_encode_tree(item))
        if type(value) is not bool:
            raise TypeError("candidate membership is not a bool")
        return value

    def __str__(self) -> str:
        value = self._aether("str")
        return value if type(value) is str else repr(self)

    def __repr__(self) -> str:
        return f"<candidate {self._aether_type}>"

    def _refuse(self, *_args: object) -> bool:
        raise TypeError(f"a candidate {self._aether_type} cannot be compared or tested for truth; compare its data")

    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _refuse  # type: ignore[assignment]
    __bool__ = _refuse  # type: ignore[assignment]
    __hash__ = object.__hash__


class _RemoteModule(types.ModuleType):
    """A candidate module in the grader: every attribute lives in the current candidate process."""

    def __getattr__(self, name: str) -> object:
        if name.startswith("__") and name.endswith("__"):
            raise AttributeError(name)
        session = _session()
        return session.call("getattr", target=session.module_handle(self.__name__), name=name)

    def __setattr__(self, name: str, value: object) -> None:
        # Import bookkeeping (dunders, submodules) stays local; everything else is the candidate's state.
        if (name.startswith("__") and name.endswith("__")) or isinstance(value, types.ModuleType):
            super().__setattr__(name, value)
            return
        session = _session()
        session.call("setattr", target=session.module_handle(self.__name__), name=name, value=_encode_tree(value))

    def __delattr__(self, name: str) -> None:
        session = _session()
        session.call("delattr", target=session.module_handle(self.__name__), name=name)


class _RemoteLoader(importlib.abc.Loader):
    def __init__(self, package: bool) -> None:
        self._package = package

    def create_module(self, spec):
        module = _RemoteModule(spec.name)
        if self._package:
            module.__path__ = []
        return module

    def exec_module(self, module) -> None:
        _session().module_handle(module.__name__)  # import it in the candidate process now


class _GraderFinder(importlib.abc.MetaPathFinder):
    """Grading modules come from the snapshot; candidate modules are proxies into the candidate process.

    The grader never compiles or executes candidate source.
    """

    def __init__(self, root: Path, sources: dict[str, bytes], modules: dict[str, tuple[str, bool]]) -> None:
        self._root = root
        self._sources = sources
        self._modules = modules

    def find_spec(self, fullname, path=None, target=None):
        entry = self._modules.get(fullname)
        if entry is None:
            return None
        name, package = entry
        if _is_candidate(name):
            if fullname.split(".", 1)[0] in sys.stdlib_module_names:
                return None
            spec = importlib.machinery.ModuleSpec(fullname, _RemoteLoader(package), is_package=package)
            if package:
                spec.submodule_search_locations = []
            return spec
        return _spec(fullname, self._root / name, self._sources[name], package)


def _not_dumpable() -> None:
    """Keep same-uid candidate processes out of /proc/<grader>/{fd,mem,environ} (best effort, Linux)."""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(4, 0, 0, 0, 0)  # PR_SET_DUMPABLE, 0
    except (OSError, AttributeError):
        pass


def _read_nonce() -> str:
    path = os.environ.pop(_NONCE_ENV, "")
    if not path:
        return ""
    try:
        return Path(path).read_text(encoding="utf-8").strip()
    except OSError:
        return ""
    finally:
        try:
            os.unlink(path)
        except OSError:
            pass


def _call_timeout() -> float:
    try:
        value = float(os.environ.get(_CALL_TIMEOUT_ENV, "") or 30)
    except ValueError:
        return 30.0
    return value if 0 < value < 3600 else 30.0


def _declared_tests(source: bytes) -> list[str] | None:
    """test_* names a test module declares at top level, read from its baseline source (None: no parse).

    These are fixed before any code runs, so nothing can hide a test or shrink the count.
    """
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return None
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.add(node.name)
        elif isinstance(node, ast.ImportFrom):
            names.update(alias.asname or alias.name for alias in node.names)
    return sorted(name for name in names if name.startswith("test_"))


def _test_timeout() -> float:
    try:
        value = float(os.environ.get(_TEST_TIMEOUT_ENV, "") or 60)
    except ValueError:
        return 60.0
    return value if 0 < value < 3600 else 60.0


def _child_env() -> dict[str, str]:
    return {key: value for key, value in os.environ.items() if not key.startswith("AETHER_PROTOCOL_")}


def _run_in_child(root: Path, request: dict[str, object], timeout: float) -> dict[str, object] | None:
    """Run one step (collect a module, or one test) in a fresh test process; its reply or None.

    The reply arrives on a pipe only that process holds, never on stdout. The test process runs trusted
    test code only; candidate code runs in its own process behind it. No reply (a crash, an early exit,
    a timeout, a malformed message) is None, which the caller records as FAIL.
    """
    request_read, request_write = os.pipe()
    reply_read, reply_write = os.pipe()
    try:
        process = subprocess.Popen(  # this interpreter running this file
            [sys.executable, "-I", str(Path(__file__).resolve()), "--runner", str(request_read), str(reply_write)],
            cwd=root,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            # Nothing a test process (or the candidate behind it) prints reaches the stdout the signed result goes to.
            stdout=sys.stderr.fileno(),
            stderr=sys.stderr.fileno(),
            pass_fds=(request_read, reply_write),
            start_new_session=True,
        )
    except OSError:
        for fd in (request_read, request_write, reply_read, reply_write):
            os.close(fd)
        return None
    os.close(request_read)
    os.close(reply_write)
    reply: object = None
    try:
        _send(request_write, request)
        os.close(request_write)
        request_write = -1
        reply = _receive(reply_read, time.monotonic() + timeout)
    except (TimeoutError, _ChannelClosed, OSError, ValueError):
        reply = None
    finally:
        for fd in (request_write, reply_read):
            if fd >= 0:
                try:
                    os.close(fd)
                except OSError:
                    pass
        # The test process, the candidate process and anything they started that stayed in the group.
        try:
            os.killpg(process.pid, 9)
        except OSError:
            pass
        process.wait()
    return reply if isinstance(reply, dict) else None


def _case(name: str, verdict: str, detail: str) -> dict[str, str]:
    return {"name": name, "verdict": verdict, "detail": detail}


def run_suite(root: Path) -> dict[str, Any]:
    """Grade the workspace. This process never imports or runs workspace code, trusted or candidate.

    Every test function runs in its own fresh test process; the test names come from the baseline
    source, and a test without a well-formed reply from its own process is a FAIL.
    """
    started = time.monotonic()
    cases: list[dict[str, str]] = []
    passed = 0
    failed = 0
    files = _files(root)
    sources = {name: path.read_bytes() for name, path in files.items()}
    baseline = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    tests = _test_modules(sources)
    encoded = {name: base64.b64encode(data).decode("ascii") for name, data in sources.items()}
    timeout = _test_timeout()
    tampered = False
    for relative, import_name, base in tests:
        if _tampered(root, baseline):
            tampered = True
            break
        label = relative[:-3].replace("/", ".")
        request: dict[str, object] = {
            "sources": encoded,
            # Only this test's own directory: bare-name helpers resolve relative to it.
            "bases": [base] if base else [],
            "relative": relative,
            "import_name": import_name,
            "label": label,
        }
        declared = _declared_tests(sources[relative]) or []
        # Importing the module once on its own: an import failure fails the module, and tests the module
        # defines without a top-level def (a loop, an assignment) are found and run as well.
        probe = _run_in_child(root, {**request, "test": None}, timeout)
        if probe is None or probe.get("kind") not in {"import_failed", "collected"}:
            failed += 1
            cases.append(_case(label, "FAIL", "import:no_result"))
            continue
        if probe["kind"] == "import_failed":
            failed += 1
            cases.append(_case(label, "FAIL", "import:" + str(probe.get("detail", ""))[:200]))
            continue
        found = probe.get("names")
        extra = [name for name in found if type(name) is str and name.startswith("test_")] if isinstance(found, list) else []
        for name in sorted(set(declared) | set(extra)):
            if _tampered(root, baseline):
                tampered = True
                break
            reply = _run_in_child(root, {**request, "test": name, "required": name in declared}, timeout)
            verdict = reply.get("verdict") if reply is not None and reply.get("kind") == "result" else None
            detail = str(reply.get("detail", ""))[:200] if reply is not None else ""
            if verdict == "SKIP":
                continue
            if verdict == "PASS":
                passed += 1
                cases.append(_case(label + "." + name, "PASS", ""))
                continue
            failed += 1
            if reply is None:
                detail = "no_result"
            elif reply.get("kind") == "import_failed":
                detail = "import:" + detail
            elif verdict != "FAIL":
                detail = "no_result"
            cases.append(_case(label + "." + name, "FAIL", detail))
        if tampered:
            break
    if tampered or _tampered(root, baseline):
        failed += 1
        cases.append(_case("suite", "FAIL", "grading_tampered"))
    if not cases:
        failed += 1
        cases.append(_case("suite", "FAIL", "no_tests"))
    return {
        "passed": passed,
        "failed": failed,
        "errors": 0,
        "cases": cases,
        "elapsed_seconds": round(time.monotonic() - started, 6),
    }


# ---------------------------------------------------------------------------------------------
# Test process. Runs one trusted test module (from the snapshot) and one of its tests; candidate
# modules are proxies into a separate candidate process, so no candidate code runs here.


def _trusted_test(value: object) -> bool:
    return callable(value) and not isinstance(value, (_Remote, _RemoteModule))


def _runner_step(root: Path, request: dict[str, Any]) -> dict[str, object]:
    sources = {name: base64.b64decode(data) for name, data in request["sources"].items()}
    bases = list(request["bases"])
    relative, import_name, label = request["relative"], request["import_name"], request["label"]
    sys.meta_path.insert(0, _GraderFinder(root, sources, _module_map(sources, bases)))
    _Current.session = _Session(root, sources, bases, _call_timeout())
    try:
        try:
            if import_name:
                module = importlib.import_module(import_name)
            else:
                # Loaded straight from the snapshot under its full dotted path, so same-named test
                # files in different directories never hide each other.
                module = importlib.util.module_from_spec(_spec(label, root / relative, sources[relative]))
                sys.modules[label] = module
                module.__spec__.loader.exec_module(module)
        except BaseException as exc:  # noqa: BLE001 - any failure to import is a FAIL verdict
            return {"kind": "import_failed", "detail": type(exc).__name__}
        name = request["test"]
        if name is None:
            names = sorted(attr for attr, value in vars(module).items() if attr.startswith("test_") and _trusted_test(value))
            return {"kind": "collected", "names": names}
        fn = vars(module).get(name)
        if not _trusted_test(fn):
            # A declared test that is gone or became a candidate object fails; a non-test name is skipped.
            return {"kind": "result", "verdict": "FAIL" if request.get("required") else "SKIP", "detail": "not_a_test"}
        try:
            value = fn()
        except BaseException as exc:  # noqa: BLE001 - any failure, even SystemExit, is a FAIL verdict
            return {"kind": "result", "verdict": "FAIL", "detail": type(exc).__name__}
        if inspect.iscoroutine(value):
            value.close()
            return {"kind": "result", "verdict": "FAIL", "detail": "async"}
        # Calling a generator function runs none of its body, so it must not count as a pass.
        if inspect.isasyncgen(value):
            return {"kind": "result", "verdict": "FAIL", "detail": "unsupported_async_generator"}
        if inspect.isgenerator(value):
            value.close()
            return {"kind": "result", "verdict": "FAIL", "detail": "unsupported_generator"}
        return {"kind": "result", "verdict": "PASS", "detail": ""}
    finally:
        _Current.session.close()
        _Current.session = None


def _runner_main(read_fd: int, write_fd: int) -> None:
    _not_dumpable()
    sys.dont_write_bytecode = True
    request = _receive(read_fd)
    os.close(read_fd)
    if not isinstance(request, dict):
        os._exit(1)
    try:
        reply = _runner_step(Path.cwd(), request)
    except BaseException:  # noqa: BLE001 - no reply is a FAIL in the grader
        os._exit(1)
    _send(write_fd, reply)
    os.close(write_fd)


# ---------------------------------------------------------------------------------------------
# Candidate side. Runs candidate code; everything it says is treated as untrusted data.


def _candidate_main(read_fd: int, write_fd: int) -> None:
    handles: dict[int, object] = {}

    def keep(value: object) -> list[object]:
        handle = len(handles) + 1
        handles[handle] = value
        return ["h", handle, type(value).__qualname__]

    def encode(value: object, depth: int = 0) -> list[object]:
        if depth > _MAX_DEPTH:
            raise ValueError("value_too_deep")
        if isinstance(value, types.ModuleType):
            return keep(value)
        return _encode_data(value, depth, encode) or keep(value)

    def decode(node: object) -> object:
        if isinstance(node, list) and node and node[0] == "h":
            return handles[node[1]]
        if isinstance(node, list) and node and node[0] in {"l", "t", "S", "F"}:
            items = [decode(item) for item in node[1]]
            return {"l": list, "t": tuple, "S": set, "F": frozenset}[node[0]](items)
        if isinstance(node, list) and node and node[0] == "d":
            return {decode(key): decode(item) for key, item in node[1]}
        return {
            "n": lambda: None,
            "b": lambda: node[1],
            "i": lambda: int(node[1]),
            "f": lambda: float(node[1]),
            "c": lambda: complex(float(node[1][0]), float(node[1][1])),
            "s": lambda: node[1],
            "y": lambda: base64.b64decode(node[1]),
        }[node[0]]()

    def handle(message: dict) -> object:
        op = message["op"]
        if op == "init":
            sources = {name: base64.b64decode(data) for name, data in message["sources"].items()}
            root = Path.cwd()
            modules = _module_map(dict.fromkeys(message["all"], b""), list(message["bases"]))
            sys.meta_path.insert(0, _CandidateFinder(root, sources, modules))
            sys.path[:0] = [*(str(root / base) for base in message["bases"]), str(root)]
            return ["n"]
        if op == "import":
            return keep(importlib.import_module(message["name"]))
        target = handles[message["target"]]
        if op == "getattr":
            return encode(getattr(target, message["name"]))
        if op == "setattr":
            setattr(target, message["name"], decode(message["value"]))
            return ["n"]
        if op == "delattr":
            delattr(target, message["name"])
            return ["n"]
        if op == "call":
            args = [decode(item) for item in message["args"]]
            kwargs = {key: decode(item) for key, item in message["kwargs"]}
            return encode(target(*args, **kwargs))
        if op == "getitem":
            return encode(target[decode(message["key"])])
        if op == "setitem":
            target[decode(message["key"])] = decode(message["value"])
            return ["n"]
        if op == "len":
            return encode(len(target))
        if op == "list":
            return encode(list(target))
        if op == "contains":
            return encode(decode(message["item"]) in target)
        if op == "str":
            return encode(str(target))
        raise ValueError("unknown_op")

    while True:
        try:
            message = _receive(read_fd)
        except _ChannelClosed:
            return
        try:
            reply: dict[str, object] = {"ok": handle(message)}  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - every candidate failure is reported, even SystemExit
            base = next(
                (kind.__name__ for kind in type(exc).__mro__ if _BUILTIN_ERRORS.get(kind.__name__) is kind),
                "CandidateError",
            )
            reply = {"err": [base, type(exc).__qualname__, str(exc)[:2000]]}
        _send(write_fd, reply)


class _CandidateFinder(importlib.abc.MetaPathFinder):
    """Candidate modules from the snapshot; grading modules (tests, oracle, helpers) cannot be imported."""

    def __init__(self, root: Path, sources: dict[str, bytes], modules: dict[str, tuple[str, bool]]) -> None:
        self._root = root
        self._sources = sources
        self._modules = modules

    def find_spec(self, fullname, path=None, target=None):
        entry = self._modules.get(fullname)
        if entry is None:
            return None
        name, package = entry
        if name not in self._sources:
            raise ImportError(f"{fullname} is part of grading and cannot be imported by candidate code")
        return _spec(fullname, self._root / name, self._sources[name], package)


def main() -> None:
    if len(sys.argv) == 4 and sys.argv[1] == "--candidate":
        _candidate_main(int(sys.argv[2]), int(sys.argv[3]))
        os._exit(0)
    if len(sys.argv) == 4 and sys.argv[1] == "--runner":
        _runner_main(int(sys.argv[2]), int(sys.argv[3]))
        sys.stdout.flush()
        os._exit(0)
    nonce = _read_nonce()
    _not_dumpable()
    body = run_suite(Path.cwd())
    if nonce:
        body["nonce"] = nonce
    sys.stdout.write(json.dumps(body, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    raise SystemExit(0 if body["failed"] == 0 and body["passed"] > 0 else 1)


if __name__ == "__main__":
    main()
