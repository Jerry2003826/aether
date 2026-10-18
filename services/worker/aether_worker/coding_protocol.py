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
  test       one fresh process per test id (a function, or a method of a Test* / TestCase class),
             plus one per module to import it and find its tests. It runs the trusted test module and
             oracle from the snapshot and replies on a pipe only it holds. A test without a well-formed
             reply (crash, early exit) is FAIL no_result; one past its limit is FAIL timeout.
  candidate  started by the test process. Candidate modules there are proxies: attribute access and
             calls cross a pipe and only data comes back (builtin values, or opaque handles that refuse
             comparison and truth), so candidate code can neither reach the test's frames, oracle or
             result, nor hand back an object that compares equal to everything. It sees only candidate
             files: its cwd is a fresh directory the grader fills with the workspace's non-grading files
             for that one test, and before any candidate code runs it locks itself (Landlock) to that
             directory, a private temp dir and the system/interpreter paths, so the grading workspace,
             tests and oracle are unreadable by any path (relative, absolute, /proc/<pid>/cwd). Without
             Landlock no candidate code runs: candidate_isolation_unavailable.
Limits (grading_files): a candidate call < a test process < the whole run, which grows with the
number of tests under a hard cap. Unsupported uses of candidate objects (isinstance on a candidate
class, except on a candidate exception class, a callback passed into candidate code, comparing a
candidate object) fail as candidate_object_unsupported:<reason>. See ADR-F44.
The grader and test processes are made non-dumpable (best effort, Linux), which keeps same-uid
processes out of their /proc fd/mem/environ and ptrace.
"""

from __future__ import annotations

import base64
import builtins
import ctypes
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import os
import select
import shutil
import signal
import stat
import struct
import subprocess
import sys
import tempfile
import time
import types
import unittest
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

_skipped = grading_files.walk_skipped


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


def _module_map(
    sources: dict[str, bytes], bases: list[str], declared: frozenset[str] | None = None
) -> dict[str, tuple[str, bool]]:
    """Import name -> (relative path, is package). The test's own non-package directory first (so a test
    there imports its sibling helper by bare name, as with pytest's default prepend mode), then the root.

    Each test process gets only its own test's directory, so same-named helpers in two test
    directories never resolve to each other, and a module added at the root cannot shadow a helper.

    A workspace module whose top-level name is a stdlib, builtin or installed-dependency name (or a
    grading_files.SHADOW_ROOTS name) is never mapped, in the test or the candidate process: `import types`
    or `import requests` always gets the interpreter's own module, never tests/types.py or ./requests.py.
    The protocol runs in the sandbox interpreter, so grading_files.shadow_names() is the grading set.
    """
    # The one exception: a top-level package that holds test modules (e.g. a `test/` package) keeps its
    # name, or its tests could not be imported at all. Only a BASELINE package (its __init__.py and a
    # test module in `declared`) qualifies, so the agent cannot claim `json` by adding json/test_x.py.
    # Without a baseline (the protocol run directly) the workspace itself is the baseline.
    listed = sources.keys() if declared is None else declared
    holders = {name.split("/", 1)[0] for name in listed if "/" in name and _is_test_module(name)}
    shadowed = grading_files.shadow_names() - {top for top in holders if f"{top}/__init__.py" in listed}
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
            if parts[0] in shadowed:
                continue
            found.setdefault(".".join(parts), (name, package))
    return found


def _declared(name: str, declared: frozenset[str] | None) -> bool:
    """A candidate module tests may import: its file, or the package `__init__.py` of a package containing
    it, existed in the baseline workspace. None (no baseline given) declares every module."""
    if declared is None or name in declared:
        return True
    parts = name.split("/")
    return any("/".join([*parts[:depth], "__init__.py"]) in declared for depth in range(1, len(parts)))


def _baseline_modules() -> frozenset[str] | None:
    """Python files of the read-only baseline the entry names (AETHER_PROTOCOL_BASELINE_DIR); listed, never run."""
    path = os.environ.pop(_BASELINE_ENV, "")
    if not path:
        return None
    return frozenset(name for name in _files(Path(path)) if name.endswith(".py"))


def _shared_memory() -> str | None:
    """The shared-memory directory the candidate may use (AETHER_PROTOCOL_SHM_DIR), or None when it is
    not named, not absolute, or not an existing directory: then the run grades nothing."""
    value = os.environ.pop(_SHM_ENV, "")
    if not value or not os.path.isabs(value) or os.path.islink(value) or not os.path.isdir(value):
        return None
    return os.path.realpath(value)


def _spec(fullname: str, origin_path: Path, data: bytes, package: bool = False):
    origin = str(origin_path)
    spec = importlib.machinery.ModuleSpec(fullname, _SnapshotLoader(data, origin), origin=origin, is_package=package)
    if package:
        spec.submodule_search_locations = [str(origin_path.parent)]
    spec.has_location = True
    return spec


_is_test_module = grading_files.is_test_module


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
_BASELINE_ENV = "AETHER_PROTOCOL_BASELINE_DIR"
# The sandbox's shared memory, which the entry has emptied after the agent stopped (it names it here);
# the candidate's semaphores, queues and shared memory live in it. Required: without it nothing is graded
# (never a fallback to /dev/shm, which nobody emptied).
_SHM_ENV = "AETHER_PROTOCOL_SHM_DIR"
_SHM_UNCONFIGURED = "shared_memory_unconfigured"
_TEST_TIMEOUT_ENV = "AETHER_PROTOCOL_TEST_TIMEOUT_SECONDS"
# The whole run's limit, set by the sandbox entry from the same formula (grading_files.protocol_timeout_seconds).
_BUDGET_ENV = "AETHER_PROTOCOL_BUDGET_SECONDS"
_RESULT_MARGIN_SECONDS = 3.0


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

    def __init__(
        self,
        root: Path,
        sources: dict[str, bytes],
        bases: list[str],
        timeout: float,
        declared: frozenset[str] | None = None,
        sandbox: dict[str, Any] | None = None,
    ) -> None:
        self._root = root
        # Where the candidate process runs and what it may touch; see _candidate_sandbox.
        self._sandbox = sandbox
        self._sources = sources
        self._bases = bases
        self._timeout = timeout
        self._declared = declared
        self._process: subprocess.Popen[bytes] | None = None
        self._to: int | None = None
        self._from: int | None = None
        self._dead: str | None = None
        self._modules: dict[str, int] = {}

    def _start(self) -> None:
        if self._sandbox is None:
            # No candidate directory: never fall back to running candidate code in the grading workspace.
            self._dead = _ISOLATION_UNAVAILABLE + ":no_candidate_dir"
            raise CandidateError(self._dead)
        child_in, to_child = os.pipe()
        from_child, child_out = os.pipe()
        env = _child_env()
        for name in ("TMPDIR", "TEMP", "TMP"):
            env[name] = str(self._sandbox["tmp"])
        try:
            self._process = subprocess.Popen(  # this interpreter running this file
                [sys.executable, "-I", str(Path(__file__).resolve()), "--candidate", str(child_in), str(child_out), str(os.getpid())],
                # The candidate's own directory, never the grading workspace.
                cwd=str(self._sandbox["src"]),
                env=env,
                stdin=subprocess.DEVNULL,
                # Candidate output is discarded: it never reaches the grader's stdout, where the signed
                # result goes, and a candidate process that outlives its test holds none of the sandbox
                # entry's pipes open.
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                pass_fds=(child_in, child_out),
                # Stays in the test process's group, which the grader kills after every test.
            )
        finally:
            os.close(child_in)
            os.close(child_out)
        self._to, self._from = to_child, from_child
        candidates = {name: base64.b64encode(data).decode("ascii") for name, data in self._sources.items() if _is_candidate(name)}
        try:
            self._request(
                {
                    "op": "init",
                    "sources": candidates,
                    "bases": self._bases,
                    "all": sorted(self._sources),
                    "declared": None if self._declared is None else sorted(self._declared),
                    "sandbox": {
                        "write": [str(self._sandbox["src"]), str(self._sandbox["tmp"])],
                        "deny": [str(item) for item in self._sandbox["deny"]],
                        "shm": str(self._sandbox["shm"]),
                    },
                }
            )
        except CandidateError:
            raise
        except Exception as exc:  # noqa: BLE001 - the init reply is a candidate error: isolation failed
            # The candidate refused to start without its filesystem lock; no candidate code has run.
            self._dead = _ISOLATION_UNAVAILABLE + ":" + str(exc)[:120]
            self.close()
            raise CandidateError(self._dead) from None

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
    # The first unsupported use of a candidate object in this test process, if any.
    unsupported: str | None = None


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


class CandidateObjectUnsupported(BaseException):
    """A test used a candidate object in a way the isolation cannot support (see ADR-F44).

    A BaseException, not a TypeError or Exception: `except TypeError`, `except Exception`,
    `pytest.raises(TypeError)` and `assertRaises(TypeError)` must not turn it into a pass. Only a bare
    `except:` or `except BaseException` can catch it, and that cannot help either: the first unsupported
    use is remembered in _Current.unsupported and fails the test whatever its outcome.
    """

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def _unsupported(reason: str) -> CandidateObjectUnsupported:
    """The error to raise, remembered so the test's FAIL detail names it even if the test catches it."""
    if _Current.unsupported is None:
        _Current.unsupported = reason
    return CandidateObjectUnsupported(reason)


def _encode_tree(value: object, depth: int = 0) -> list[object]:
    encoded = _encode_trusted(value, depth)
    if not encoded:
        # A function, class or other trusted object cannot cross into the candidate process.
        raise _unsupported("callable_passed_to_candidate" if callable(value) else "object_passed_to_candidate")
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
        raise _unsupported("compare_candidate_object")

    def __bool__(self) -> bool:
        raise _unsupported("truth_of_candidate_object")

    # isinstance(x, candidate_class) / issubclass(...) look these up on the type of the class argument.
    def __instancecheck__(self, _instance: object) -> bool:
        raise _unsupported("isinstance_candidate_class")

    def __subclasscheck__(self, _subclass: object) -> bool:
        raise _unsupported("issubclass_candidate_class")

    __eq__ = __ne__ = __lt__ = __le__ = __gt__ = __ge__ = _refuse  # type: ignore[assignment]
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

    def __init__(
        self,
        root: Path,
        sources: dict[str, bytes],
        modules: dict[str, tuple[str, bool]],
        declared: frozenset[str] | None = None,
    ) -> None:
        self._root = root
        self._sources = sources
        self._modules = modules
        self._declared = declared

    def find_spec(self, fullname, path=None, target=None):
        entry = self._modules.get(fullname)
        if entry is None:
            return None
        name, package = entry
        # Tests only get modules the baseline declared; a module the agent added is for candidate code
        # (which imports it in the candidate process), not a new name for tests.
        if not _declared(name, self._declared):
            raise ImportError(f"{fullname} is not a baseline module", name=fullname)
        if _is_candidate(name):
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


def _seconds(name: str, default: float, ceiling: float) -> float:
    """A limit from the environment (tests shorten them); never above the documented ceiling."""
    try:
        value = float(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return value if 0 < value <= ceiling else default


def _test_timeout() -> float:
    return _seconds(_TEST_TIMEOUT_ENV, grading_files.PROTOCOL_TEST_TIMEOUT_SECONDS, grading_files.PROTOCOL_TEST_TIMEOUT_SECONDS)


def _call_timeout(test_timeout: float) -> float:
    """One call into candidate code ends before its test process does, so a hang is reported as such."""
    configured = _seconds(_CALL_TIMEOUT_ENV, grading_files.PROTOCOL_CALL_TIMEOUT_SECONDS, grading_files.PROTOCOL_CALL_TIMEOUT_SECONDS)
    return min(configured, test_timeout * 0.75)


# Name fragments of credentials a child must never inherit (upper-cased match).
_CREDENTIAL_MARKERS = ("API_KEY", "TOKEN", "SECRET", "PASSWORD", "CREDENTIAL", "ACCESS_KEY", "PRIVATE_KEY", "DATABASE_URL")


def _child_env() -> dict[str, str]:
    """Environment for the candidate and test processes: no AETHER_* settings and nothing credential-like.

    The agent phase ran with the model channel (AETHER_JOB_TOKEN, AETHER_MODEL_SOCKET, ...); none of
    it, nor any other credential, may reach candidate code while it is graded, or that code could
    notice grading and call the paid model. The protocol's own settings never travel by env either.
    """
    return {
        key: value
        for key, value in os.environ.items()
        if not key.upper().startswith("AETHER_") and not any(marker in key.upper() for marker in _CREDENTIAL_MARKERS)
    }


_ISOLATION_UNAVAILABLE = "candidate_isolation_unavailable"
# Candidate (non-grading) files copied into each candidate directory; past this the rest are left out
# (and logged by name). Copied once per step, so the bound keeps the copy far inside the protocol budget:
# a full 8 MiB (1000 files) onto the sandbox's tmpfs measured ~30 ms per step, ~3 s over the ~105
# steps the 240 s cap allows (each step is allowed 2 s), and it counts once against container memory. Source trees are far smaller; data sets that are
# not tests do not belong in a graded workspace.
_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
_DROPPED_NAMES_LOGGED = 20


def _candidate_files(root: Path) -> dict[str, bytes]:
    """The workspace files candidate code may see: regular, non-grading files outside skipped dirs.

    Read once, before any candidate code runs, and written into a fresh directory for every test.
    """
    found: dict[str, bytes] = {}
    dropped: list[str] = []
    total = 0
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
            if not path.is_file() or grading_files.is_grading_path(name):
                continue
            try:
                data = path.read_bytes()
            except OSError:
                continue
            if total + len(data) > _MAX_CANDIDATE_BYTES:
                dropped.append(name)
                continue
            total += len(data)
            found[name] = data
    if dropped:
        # Names and a count on the grader's stderr (the entry's log), never contents; not the result line.
        names = ", ".join(repr(name[:200]) for name in dropped[:_DROPPED_NAMES_LOGGED])
        more = f" (+{len(dropped) - _DROPPED_NAMES_LOGGED} more)" if len(dropped) > _DROPPED_NAMES_LOGGED else ""
        sys.stderr.write(f"aether-protocol: candidate_files_dropped count={len(dropped)} limit_bytes={_MAX_CANDIDATE_BYTES} names=[{names}]{more}\n")
        sys.stderr.flush()
    return found


def _candidate_sandbox(files: dict[str, bytes], deny: list[str], shm: str) -> dict[str, Any]:
    """A fresh directory for one test's candidate process: src/ (its cwd, the candidate files) and tmp/."""
    top = Path(tempfile.mkdtemp(prefix="aether-candidate-"))
    src, tmp = top / "src", top / "tmp"
    src.mkdir(mode=0o700)
    tmp.mkdir(mode=0o700)
    for name, data in files.items():
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return {"top": str(top), "src": str(src), "tmp": str(tmp), "deny": deny, "shm": shm}


def _remove_tree(top: str) -> None:
    """Remove a candidate directory, whatever modes candidate code left on it (it never follows links)."""

    def unlock(_function: object, path: str, _error: BaseException) -> None:
        try:
            os.chmod(os.path.dirname(path), stat.S_IRWXU)
            os.chmod(path, stat.S_IRWXU, follow_symlinks=False)
        except (OSError, NotImplementedError):
            pass
        try:
            if os.path.isdir(path) and not os.path.islink(path):
                shutil.rmtree(path, ignore_errors=True)
            else:
                os.unlink(path)
        except OSError:
            pass

    shutil.rmtree(top, onexc=unlock)


# Landlock (Linux >= 5.13). Syscall numbers are the same on every architecture.
_LANDLOCK_CREATE_RULESET = 444
_LANDLOCK_ADD_RULE = 445
_LANDLOCK_RESTRICT_SELF = 446
_LANDLOCK_CREATE_RULESET_VERSION = 1
_LANDLOCK_RULE_PATH_BENEATH = 1
_PR_SET_NO_NEW_PRIVS = 38
_FS_EXECUTE = 1 << 0
_FS_WRITE_FILE = 1 << 1
_FS_READ_FILE = 1 << 2
_FS_READ_DIR = 1 << 3
_FS_REFER = 1 << 13  # ABI 2
_FS_TRUNCATE = 1 << 14  # ABI 3
_FS_IOCTL_DEV = 1 << 15  # ABI 5
# ABI 6 scopes: no connecting to abstract unix sockets, and no signals, outside the candidate's domain
# (the test process, the grader, the sandbox entry).
_SCOPE_ABSTRACT_UNIX_SOCKET = 1 << 0
_SCOPE_SIGNAL = 1 << 1
_FS_FILE_ONLY = _FS_EXECUTE | _FS_WRITE_FILE | _FS_READ_FILE | _FS_TRUNCATE | _FS_IOCTL_DEV
# Read and run: the interpreter, its libraries and the system. Never /tmp (job workspaces live there).
_READ_PATHS = ("/usr", "/lib", "/lib32", "/lib64", "/libx32", "/bin", "/sbin", "/etc", "/opt", "/proc")


def _handled_access(abi: int) -> int:
    handled = (1 << 13) - 1
    if abi >= 2:
        handled |= _FS_REFER
    if abi >= 3:
        handled |= _FS_TRUNCATE
    if abi >= 5:
        handled |= _FS_IOCTL_DEV
    return handled


def _within(path: str, parent: str) -> bool:
    return path == parent or path.startswith(parent.rstrip("/") + "/")


def _sandbox_rules(write: list[str], deny: list[str], shm: str) -> list[tuple[str, int]]:
    """(path, rights) for the candidate process. A read path that holds a denied one is refused.

    `shm` is the sandbox's shared memory: full access (POSIX semaphores and shared memory need it), and
    safe only because the entry empties it after the agent stopped, before any candidate code runs, so
    nothing the agent left there (a copy of a test or the oracle) is still in it. One that overlaps a
    denied path (the grading workspace, the baseline) is refused like a read path.
    """
    read = _FS_EXECUTE | _FS_READ_FILE | _FS_READ_DIR
    interpreter = {sys.prefix, sys.exec_prefix, sys.base_prefix, sys.base_exec_prefix}
    interpreter |= {entry for entry in sys.path if entry and os.path.isabs(entry)}
    paths = sorted({os.path.realpath(item) for item in (*_READ_PATHS, *interpreter)})
    denied = [os.path.realpath(item) for item in deny]
    rules: list[tuple[str, int]] = []
    for path in paths:
        for blocked in denied:
            if _within(blocked, path) or _within(path, blocked):
                # e.g. an interpreter installed inside the workspace: fail closed rather than expose it.
                raise PermissionError(f"read path {path} overlaps the grading workspace")
        rules.append((path, read))
    rules.append(("/dev", read | _FS_WRITE_FILE | _FS_TRUNCATE | _FS_IOCTL_DEV))
    shared = os.path.realpath(shm)
    for blocked in denied:
        if _within(blocked, shared) or _within(shared, blocked):
            raise PermissionError(f"shared memory {shared} overlaps the grading workspace")
    rules.append((shared, -1))
    rules.extend((os.path.realpath(path), -1) for path in write)
    return rules


def _landlock(write: list[str], deny: list[str], shm: str) -> None:
    """Lock this process (and everything it starts) to the system, the interpreter and `write`; with
    Landlock ABI 6 it also cannot signal, or reach abstract unix sockets of, any process outside its domain.

    Raises OSError when Landlock is unavailable; the caller then runs no candidate code at all.
    """
    rules = _sandbox_rules(write, deny, shm)
    libc = ctypes.CDLL(None, use_errno=True)
    syscall = libc.syscall
    syscall.restype = ctypes.c_long

    def check(result: int, what: str) -> int:
        if result < 0:
            error = ctypes.get_errno()
            raise OSError(error, f"landlock {what}: {os.strerror(error)}")
        return result

    abi = check(syscall(ctypes.c_long(_LANDLOCK_CREATE_RULESET), None, ctypes.c_size_t(0), ctypes.c_uint32(_LANDLOCK_CREATE_RULESET_VERSION)), "abi")
    handled = _handled_access(abi)
    if abi >= 6:
        # handled_access_fs, handled_access_net (none: the sandbox has no network), scoped.
        packed = struct.pack("=QQQ", handled, 0, _SCOPE_ABSTRACT_UNIX_SOCKET | _SCOPE_SIGNAL)
    else:
        # Older kernels: the filesystem lock only; the candidate can still signal same-uid processes.
        packed = struct.pack("=Q", handled)
    attr = ctypes.create_string_buffer(packed, len(packed))
    ruleset = check(
        syscall(ctypes.c_long(_LANDLOCK_CREATE_RULESET), attr, ctypes.c_size_t(len(packed)), ctypes.c_uint32(0)), "create_ruleset"
    )
    try:
        for path, rights in rules:
            try:
                fd = os.open(path, os.O_PATH | os.O_CLOEXEC)
            except OSError:
                continue  # absent on this system
            try:
                allowed = handled if rights < 0 else rights & handled
                if not stat.S_ISDIR(os.fstat(fd).st_mode):
                    allowed &= _FS_FILE_ONLY
                beneath = ctypes.create_string_buffer(struct.pack("=Qi", allowed, fd), 12)
                check(syscall(ctypes.c_long(_LANDLOCK_ADD_RULE), ctypes.c_int(ruleset), ctypes.c_int(_LANDLOCK_RULE_PATH_BENEATH), beneath, ctypes.c_uint32(0)), "add_rule")
            finally:
                os.close(fd)
        check(libc.prctl(_PR_SET_NO_NEW_PRIVS, 1, 0, 0, 0), "no_new_privs")
        check(syscall(ctypes.c_long(_LANDLOCK_RESTRICT_SELF), ctypes.c_int(ruleset), ctypes.c_uint32(0)), "restrict_self")
    finally:
        os.close(ruleset)


_PR_SET_PDEATHSIG = 1


def _die_with_parent(parent: int) -> None:
    """SIGKILL this process when its parent dies (Linux), so a killed grader leaves nothing behind."""
    try:
        import ctypes

        ctypes.CDLL(None, use_errno=True).prctl(_PR_SET_PDEATHSIG, signal.SIGKILL, 0, 0, 0)
    except (OSError, AttributeError):
        pass
    if os.getppid() != parent:
        os._exit(1)


# Process groups of the test processes still running; killed on every way out of the grader.
_LIVE: set[int] = set()


def _kill_live() -> None:
    for group in list(_LIVE):
        try:
            os.killpg(group, signal.SIGKILL)
        except OSError:
            pass
        _LIVE.discard(group)


def _on_signal(signum: int, _frame: object) -> None:
    _kill_live()
    os._exit(128 + signum)


_TIMED_OUT = {"kind": "result", "verdict": "FAIL", "detail": "timeout"}


def _run_in_child(
    root: Path, request: dict[str, object], timeout: float, candidate: tuple[dict[str, bytes], list[str], str]
) -> dict[str, object] | None:
    """Run one step (collect a module, or one test) in a fresh test process; its reply or None.

    The reply arrives on a pipe only that process holds, never on stdout. The test process runs trusted
    test code only; candidate code runs in its own process behind it, in a fresh candidate directory
    (`candidate` = its files and the paths it must not read) that is removed after the step. A timeout
    is a FAIL "timeout" result; no reply (a crash, an early exit, a malformed message) is None, also a FAIL.
    """
    try:
        sandbox = _candidate_sandbox(*candidate)
    except OSError:
        return None
    try:
        return _run_test_process(root, {**request, "sandbox": sandbox}, timeout)
    finally:
        _remove_tree(sandbox["top"])


def _run_test_process(root: Path, request: dict[str, object], timeout: float) -> dict[str, object] | None:
    request_read, request_write = os.pipe()
    reply_read, reply_write = os.pipe()
    try:
        process = subprocess.Popen(  # this interpreter running this file
            [sys.executable, "-I", str(Path(__file__).resolve()), "--runner", str(request_read), str(reply_write), str(os.getpid())],
            cwd=root,
            env=_child_env(),
            stdin=subprocess.DEVNULL,
            # Nothing a test process prints reaches the stdout the signed result goes to.
            stdout=sys.stderr.fileno(),
            stderr=sys.stderr.fileno(),
            pass_fds=(request_read, reply_write),
            start_new_session=True,
        )
    except OSError:
        for fd in (request_read, request_write, reply_read, reply_write):
            os.close(fd)
        return None
    _LIVE.add(process.pid)
    os.close(request_read)
    os.close(reply_write)
    reply: object = None
    try:
        _send(request_write, request)
        os.close(request_write)
        request_write = -1
        reply = _receive(reply_read, time.monotonic() + timeout)
    except TimeoutError:
        reply = dict(_TIMED_OUT)
    except (_ChannelClosed, OSError, ValueError):
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
            os.killpg(process.pid, signal.SIGKILL)
        except OSError:
            pass
        process.wait()
        _LIVE.discard(process.pid)
    return reply if isinstance(reply, dict) else None


def _case(name: str, verdict: str, detail: str) -> dict[str, str]:
    return {"name": name, "verdict": verdict, "detail": detail}


def _deadline(steps: int) -> float:
    """When the grader stops starting tests: the entry's budget for this run (it sizes it from the same
    formula), less a margin for writing the result, and never more than the formula itself."""
    limit = float(grading_files.protocol_timeout_seconds(steps))
    budget = _seconds(_BUDGET_ENV, limit, grading_files.PROTOCOL_CAP_SECONDS)
    return time.monotonic() + max(1.0, min(limit, budget) - _RESULT_MARGIN_SECONDS)


def _probe_ids(probe: dict[str, object]) -> tuple[list[str], dict[str, str]]:
    found = probe.get("names")
    names = [name for name in found if type(name) is str and name] if isinstance(found, list) else []
    raw = probe.get("unsupported")
    unsupported = {key: str(value)[:200] for key, value in raw.items() if type(key) is str} if isinstance(raw, dict) else {}
    return names, unsupported


def run_suite(root: Path) -> dict[str, Any]:
    """Grade the workspace. This process never imports or runs workspace code, trusted or candidate.

    Every test (function, or method of a test class) runs in its own fresh test process; the test ids
    come from the baseline source, and a test without a well-formed reply from its own process is a FAIL.
    """
    started = time.monotonic()
    shared_memory = _shared_memory()
    if shared_memory is None:
        # Before anything else, and before any test or candidate process exists.
        return {"passed": 0, "failed": 1, "errors": 0, "cases": [_case("suite", "FAIL", _SHM_UNCONFIGURED)], "elapsed_seconds": 0.0}
    cases: list[dict[str, str]] = []
    passed = 0
    failed = 0
    files = _files(root)
    sources = {name: path.read_bytes() for name, path in files.items()}
    baseline_modules = _baseline_modules()
    baseline = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    tests = _test_modules(sources)
    encoded = {name: base64.b64encode(data).decode("ascii") for name, data in sources.items()}
    # What candidate code may see, read before any of it runs; and what it must never read.
    candidate = (_candidate_files(root), [str(root), os.environ.get("AETHER_SANDBOX_WORK", "/work")], shared_memory)
    declared_by_module = {relative: grading_files.declared_tests(sources[relative]) or ([], {}) for relative, _, _ in tests}
    deadline = _deadline(sum(1 + len(ids) for ids, _ in declared_by_module.values()))
    test_timeout = _test_timeout()
    call_timeout = _call_timeout(test_timeout)
    tampered = False

    def remaining() -> float:
        return min(test_timeout, deadline - time.monotonic())

    for relative, import_name, base in tests:
        if _tampered(root, baseline):
            tampered = True
            break
        label = relative[:-3].replace("/", ".")
        declared, unsupported = declared_by_module[relative]
        if remaining() <= 0:
            # Out of time before this module started: every declared test is a timed-out failure.
            for name in [*declared, *unsupported] or [""]:
                failed += 1
                cases.append(_case(label + ("." + name if name else ""), "FAIL", "timeout"))
            continue
        request: dict[str, object] = {
            "sources": encoded,
            # Only this test's own directory: bare-name helpers resolve relative to it.
            "bases": [base] if base else [],
            "relative": relative,
            "import_name": import_name,
            "label": label,
            "call_timeout": call_timeout,
            "declared": None if baseline_modules is None else sorted(baseline_modules),
        }
        # Importing the module once on its own: an import failure fails the module, and tests the module
        # defines without a top-level def (a loop, an assignment, an imported test class) are run as well.
        probe = _run_in_child(root, {**request, "test": None}, remaining(), candidate)
        if probe is None or probe.get("kind") not in {"import_failed", "collected", "result"}:
            failed += 1
            cases.append(_case(label, "FAIL", "import:no_result"))
            continue
        if probe["kind"] == "result":
            failed += 1
            cases.append(_case(label, "FAIL", "import:" + str(probe.get("detail", ""))[:200]))
            continue
        if probe["kind"] == "import_failed":
            failed += 1
            cases.append(_case(label, "FAIL", "import:" + str(probe.get("detail", ""))[:200]))
            continue
        found, found_unsupported = _probe_ids(probe)
        unsupported = {**found_unsupported, **unsupported}
        # Classes that cannot be run are failures, counted, never dropped.
        for name in sorted(unsupported):
            failed += 1
            cases.append(_case(label + "." + name, "FAIL", unsupported[name]))
        for name in sorted((set(declared) | set(found)) - set(unsupported)):
            if _tampered(root, baseline):
                tampered = True
                break
            budget = remaining()
            if budget <= 0:
                failed += 1
                cases.append(_case(label + "." + name, "FAIL", "timeout"))
                continue
            reply = _run_in_child(root, {**request, "test": name, "required": name in declared}, budget, candidate)
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


def _trusted_class(value: object) -> bool:
    return isinstance(value, type) and not issubclass(value, (_Remote, _RemoteModule))


def _result(verdict: str, detail: str = "") -> dict[str, object]:
    return {"kind": "result", "verdict": verdict, "detail": detail}


def _failure_detail(exc: BaseException) -> str:
    if isinstance(exc, CandidateError) and str(exc) == "candidate_timeout":
        return "timeout"
    if isinstance(exc, CandidateError) and str(exc).startswith(_ISOLATION_UNAVAILABLE):
        return _ISOLATION_UNAVAILABLE
    if isinstance(exc, CandidateObjectUnsupported):
        return "candidate_object_unsupported:" + exc.reason
    if isinstance(exc, TypeError) and str(exc).startswith("catching classes that do not inherit from BaseException"):
        # `except candidate.SomeError:`: the class is a proxy, never a real exception class.
        return "candidate_object_unsupported:except_candidate_exception_class"
    if _Current.unsupported is not None:
        return "candidate_object_unsupported:" + _Current.unsupported
    return type(exc).__name__


def _call_test(fn: object) -> dict[str, object]:
    try:
        value = fn()  # type: ignore[operator]
    except BaseException as exc:  # noqa: BLE001 - any failure, even SystemExit, is a FAIL verdict
        return _result("FAIL", _failure_detail(exc))
    if inspect.iscoroutine(value):
        value.close()
        return _result("FAIL", "async")
    # Calling a generator function runs none of its body, so it must not count as a pass.
    if inspect.isasyncgen(value):
        return _result("FAIL", "unsupported_async_generator")
    if inspect.isgenerator(value):
        value.close()
        return _result("FAIL", "unsupported_generator")
    return _result("PASS")


class _CaseResult(unittest.TestResult):
    """Keeps the type of the first failure; skips and expected failures are never a pass."""

    def __init__(self) -> None:
        super().__init__()
        self.detail = ""

    def _note(self, err) -> None:
        if not self.detail:
            self.detail = _failure_detail(err[1]) if err[1] is not None else err[0].__name__

    def addError(self, test, err) -> None:  # noqa: N802 - unittest API
        super().addError(test, err)
        self._note(err)

    def addFailure(self, test, err) -> None:  # noqa: N802 - unittest API
        super().addFailure(test, err)
        self._note(err)

    def addSubTest(self, test, subtest, err) -> None:  # noqa: N802 - unittest API
        super().addSubTest(test, subtest, err)
        if err is not None:
            self._note(err)


def _takes_argument(hook: object) -> bool:
    try:
        return bool(inspect.signature(hook).parameters)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return False


def _call_hook(hook: object, argument: object) -> None:
    hook(argument) if _takes_argument(hook) else hook()  # type: ignore[operator]


def _with_xunit(module: types.ModuleType, setup: str, teardown: str, argument: object, run) -> dict[str, object]:
    """pytest-style setup_<x>/teardown_<x> around `run`. A failing setup fails the test (and its
    teardown does not run, as with pytest); a failing teardown fails a test that had passed."""
    hook = vars(module).get(setup)
    if hook is not None and _trusted_test(hook):
        try:
            _call_hook(hook, argument)
        except BaseException as exc:  # noqa: BLE001 - a failing fixture fails the test
            return _result("FAIL", _failure_detail(exc))
    outcome = run()
    closer = vars(module).get(teardown)
    if closer is not None and _trusted_test(closer):
        try:
            _call_hook(closer, argument)
        except BaseException as exc:  # noqa: BLE001 - a failing teardown fails the test
            if outcome["verdict"] == "PASS":
                outcome = _result("FAIL", _failure_detail(exc))
    return outcome


def _run_case_method(cls: type, method: str) -> dict[str, object]:
    fn = getattr(cls, method, None)
    if not issubclass(cls, unittest.IsolatedAsyncioTestCase):
        if inspect.iscoroutinefunction(fn):
            return _result("FAIL", "async")
        if inspect.isasyncgenfunction(fn):
            return _result("FAIL", "unsupported_async_generator")
    if inspect.isgeneratorfunction(fn):
        return _result("FAIL", "unsupported_generator")
    try:
        case = cls(method)
    except BaseException as exc:  # noqa: BLE001 - a case that cannot be built fails
        return _result("FAIL", _failure_detail(exc))
    outcome = _CaseResult()
    # Through a suite, so setUpModule/setUpClass and tearDownClass/tearDownModule run as with unittest;
    # case.run() alone would skip them. The suite finds module fixtures through sys.modules.
    unittest.TestSuite([case]).run(outcome)
    if outcome.detail:
        return _result("FAIL", outcome.detail)
    if outcome.skipped:
        return _result("FAIL", "skipped")
    if outcome.expectedFailures:
        return _result("FAIL", "expected_failure")
    if outcome.unexpectedSuccesses:
        return _result("FAIL", "unexpected_success")
    if outcome.testsRun != 1 or not outcome.wasSuccessful():
        return _result("FAIL", "no_result")
    return _result("PASS")


def _run_class_method(cls: type, method: str) -> dict[str, object]:
    """A pytest-style Test* class: a fresh instance; setup_class/setup_method and their teardowns run.

    As with pytest, each teardown runs whether or not its setup is defined; only a failing setup
    stops them (the setup error is the verdict and nothing else runs)."""
    if "__init__" in {name for klass in cls.__mro__[:-1] for name in vars(klass)}:
        return _result("FAIL", "unsupported_test_class:init")
    fn = getattr(cls, method, None)
    if not _trusted_test(fn):
        return _result("FAIL", "not_a_test")
    try:
        instance = cls()
        bound = getattr(instance, method)
        for name, args in (("setup_class", ()), ("setup_method", (bound,))):
            hook = getattr(cls if name == "setup_class" else instance, name, None)
            if callable(hook):
                hook(*args)
    except BaseException as exc:  # noqa: BLE001 - a failing fixture fails the test
        return _result("FAIL", _failure_detail(exc))
    outcome = _call_test(bound)
    for name in ("teardown_method", "teardown_class"):
        hook = getattr(cls if name == "teardown_class" else instance, name, None)
        if not callable(hook):
            continue
        try:
            hook(bound) if name == "teardown_method" else hook()
        except BaseException as exc:  # noqa: BLE001 - a failing teardown fails the test
            if outcome["verdict"] == "PASS":
                outcome = _result("FAIL", _failure_detail(exc))
    return outcome


def _collect(module: types.ModuleType) -> dict[str, object]:
    """Tests the imported module exposes: functions, and methods of test classes (own or imported)."""
    names: set[str] = set()
    unsupported: dict[str, str] = {}
    for attr, value in vars(module).items():
        if attr.startswith("test_") and _trusted_test(value) and not isinstance(value, type):
            names.add(attr)
            continue
        if not _trusted_class(value) or getattr(value, "__test__", True) is False:
            continue
        own = getattr(value, "__module__", None) == module.__name__
        if issubclass(value, unittest.TestCase):
            names.update(f"{attr}.{method}" for method in unittest.TestLoader().getTestCaseNames(value))
        elif attr.startswith("Test"):
            if "__init__" in {name for klass in value.__mro__[:-1] for name in vars(klass)}:
                # A class this module defines must run; one it merely imports (e.g. a test client) is not a test.
                if own:
                    unsupported[attr] = "unsupported_test_class:init"
                continue
            names.update(f"{attr}.{method}" for method in dir(value) if method.startswith("test") and callable(getattr(value, method, None)))
    return {"kind": "collected", "names": sorted(names), "unsupported": unsupported}


def _run_test(module: types.ModuleType, name: str, required: bool) -> dict[str, object]:
    owner, _, method = name.rpartition(".")
    if owner:
        cls = vars(module).get(owner)
        if not _trusted_class(cls):
            return _result("FAIL", "not_a_test")
        if issubclass(cls, unittest.TestCase):
            return _run_case_method(cls, method)
        return _with_xunit(module, "setup_module", "teardown_module", module, lambda: _run_class_method(cls, method))
    fn = vars(module).get(name)
    if not _trusted_test(fn) or isinstance(fn, type):
        # A declared test that is gone or became a candidate object fails; a non-test name is skipped.
        return _result("FAIL" if required else "SKIP", "not_a_test")
    return _with_xunit(
        module,
        "setup_module",
        "teardown_module",
        module,
        lambda: _with_xunit(module, "setup_function", "teardown_function", fn, lambda: _call_test(fn)),
    )


def _runner_step(root: Path, request: dict[str, Any]) -> dict[str, object]:
    sources = {name: base64.b64decode(data) for name, data in request["sources"].items()}
    bases = list(request["bases"])
    relative, import_name, label = request["relative"], request["import_name"], request["label"]
    listed = request.get("declared")
    declared = None if listed is None else frozenset(listed)
    sys.meta_path.insert(0, _GraderFinder(root, sources, _module_map(sources, bases, declared), declared))
    sandbox = request.get("sandbox")
    _Current.session = _Session(
        root, sources, bases, float(request["call_timeout"]), declared, sandbox if isinstance(sandbox, dict) else None
    )
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
            return {"kind": "import_failed", "detail": _failure_detail(exc)}
        name = request["test"]
        if name is None:
            return _collect(module)
        outcome = _run_test(module, name, bool(request.get("required")))
        if outcome["verdict"] != "SKIP" and _Current.unsupported is not None:
            # Any unsupported use of a candidate object fails the test, even if the test caught the error.
            return _result("FAIL", "candidate_object_unsupported:" + _Current.unsupported)
        return outcome
    finally:
        _Current.session.close()
        _Current.session = None


def _runner_main(read_fd: int, write_fd: int, parent: int) -> None:
    _die_with_parent(parent)
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
            listed = message.get("declared")
            modules = _module_map(
                dict.fromkeys(message["all"], b""), list(message["bases"]), None if listed is None else frozenset(listed)
            )
            sys.meta_path.insert(0, _CandidateFinder(root, sources, modules))
            # Appended, after the stdlib and site-packages: a workspace file the module map refuses (a
            # stdlib or dependency name) cannot come back in through the path finder.
            sys.path.extend([*(str(root / base) for base in message["bases"]), str(root)])
            sandbox = message["sandbox"]
            # Last, and before any candidate code: from here on only the system, the interpreter and
            # this candidate directory exist for this process and anything it starts.
            _landlock(list(sandbox["write"]), list(sandbox["deny"]), str(sandbox["shm"]))
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
    if len(sys.argv) == 5 and sys.argv[1] == "--candidate":
        _die_with_parent(int(sys.argv[4]))
        _candidate_main(int(sys.argv[2]), int(sys.argv[3]))
        os._exit(0)
    if len(sys.argv) == 5 and sys.argv[1] == "--runner":
        _runner_main(int(sys.argv[2]), int(sys.argv[3]), int(sys.argv[4]))
        sys.stdout.flush()
        os._exit(0)
    nonce = _read_nonce()
    _not_dumpable()
    # Every way out of the grader stops the test processes it started (their candidate processes die
    # with them): a signal kills them and exits with no result line, which the entry reports as
    # grading_timeout; a SIGKILL cannot be caught, so each child also dies with its parent.
    for signum in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP):
        signal.signal(signum, _on_signal)
    try:
        body = run_suite(Path.cwd())
    finally:
        _kill_live()
    if nonce:
        body["nonce"] = nonce
    sys.stdout.write(json.dumps(body, separators=(",", ":")) + "\n")
    sys.stdout.flush()
    raise SystemExit(0 if body["failed"] == 0 and body["passed"] > 0 else 1)


if __name__ == "__main__":
    main()
