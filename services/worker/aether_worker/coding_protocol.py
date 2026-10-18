"""Grading protocol executed by OpenHands inside the sandbox. It does not score or import OpenHands.
The trusted suite is collected and run by pytest (in the test processes only), under the isolation below.

Integrity: before any candidate code runs, every Python source and grading file under the workspace
(virtualenvs with pyvenv.cfg, node_modules, VCS and cache dirs skipped) is read into memory and hashed. All Python imports from the workspace (test modules, oracle.py, candidate code, packages)
are served from that in-memory snapshot, so a file rewritten during the run has no effect on any
module loaded afterwards. Those files are hashed again before each test and after the run;
one that is missing or changed fails the suite with `grading_tampered`. New files are fine.

Isolation: three kinds of process, and candidate code only ever runs in the last one.
  grader     this process. Reads (and deletes) the nonce file first, takes the snapshot, reads the
             test names from the baseline source, and aggregates verdicts. It never imports or runs
             workspace code. Its one stdout line, signed with the nonce, is the result.
  test       one fresh process per test module. It imports the trusted test module and oracle from the
             snapshot and runs pytest in-process on that module object (pytest never imports the file,
             and reads no config, conftest, plugin or PYTEST_* variable), so collection, fixtures and
             unittest/xunit scopes are pytest's own. It replies per test on a pipe only it holds. A test
             without a well-formed reply (crash, early exit) is FAIL no_result, one past its limit FAIL
             timeout; the module's remaining tests then run in a fresh test process.
  candidate  started by the test process. Candidate modules there are proxies: attribute access and
             calls cross a pipe and only data comes back (builtin values, or opaque handles that refuse
             comparison and truth; an instance of a builtin-type subclass is a handle that compares as
             its builtin data unless its class defines that comparison), so candidate code can neither
             reach the test's frames, oracle or result, nor hand back an object that compares equal to
             everything. It sees only candidate
             files: its cwd is a fresh directory the grader fills with the workspace's non-grading files
             for that one test process, and before any candidate code runs it locks itself (Landlock) to that
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
import collections
import ctypes
import hashlib
import importlib.abc
import importlib.machinery
import importlib.util
import inspect
import json
import math
import operator
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
# Root helpers the BASELINE marks as grading (grading_files.marked_grading_files): set by the grader
# from AETHER_PROTOCOL_BASELINE_DIR, and handed to every test process in its request. Never read from
# the graded workspace, where the agent could have removed or added the marker.
_MARKED: frozenset[str] = frozenset()


def _is_grading(name: str) -> bool:
    return grading_files.is_grading_path(name, marked=_MARKED)


def _guarded(name: str) -> bool:
    """Snapshot and verify Python sources and grading files; other data may change freely."""
    return name.endswith(".py") or _is_grading(name)


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
    global _MARKED
    path = os.environ.pop(_BASELINE_ENV, "")
    if not path:
        return None
    _MARKED = grading_files.marked_grading_files(Path(path))
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
    return name.endswith(".py") and not _is_grading(name)


def _test_modules(sources: dict[str, bytes]) -> list[tuple[str, str, str]]:
    """(relative path, import name or "" for a direct load, base dir) of every test module, root first."""
    found: list[tuple[str, str, str]] = []
    for name in sources:
        if not _is_test_module(name) or _is_candidate(name):
            continue
        parts = name[:-3].split("/")
        # Climb while the parent directory is a package; the first non-package directory is the base.
        depth = len(parts) - 1
        while depth > 0 and "/".join(parts[:depth]) + "/__init__.py" in sources:
            depth -= 1
        base = "/".join(parts[:depth])
        # A package under a non-package base is imported through the finder (relative imports work);
        # a plain module there is loaded directly under its full dotted path. A path that is no import
        # name (test_bad-file.py) is loaded directly too: pytest collects it, so it is never dropped.
        importable = (depth == 0 or depth < len(parts) - 1) and all(part.isidentifier() for part in parts)
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
    """One candidate process. Started lazily; a new one for every test process (one per test module, and per resume)."""

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
        # Shared builtin containers (see _MIRRORED): id -> the test's own object. The test allocates
        # positive ids, the candidate negative ones; both sides keep the last state exchanged.
        self._mirrors: dict[int, object] = {}
        self._mirror_ids: dict[int, int] = {}
        self._synced: dict[int, str] = {}
        # One proxy per candidate handle, so the same candidate object is the same object to the test.
        self._remotes: dict[int, _Remote] = {}

    def remote(self, handle: int, make) -> _Remote:
        found = self._remotes.get(handle)
        if found is None:
            found = self._remotes[handle] = make()
        return found

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
        _Spawn.allowed = True
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
            _Spawn.allowed = False
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
        if message.get("op") != "init":
            message = {**message, "sync": self._outgoing()}
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
        # The candidate's changes to shared containers land before its result or its exception.
        try:
            self._incoming(reply.get("sync", []))
            if "err" in reply:
                raw = reply["err"]
                if not (isinstance(raw, list) and len(raw) == 3 and all(type(item) is str for item in raw)):
                    raise CandidateError("candidate_protocol")
                error = _exception(raw)
            else:
                return _decode(reply.get("ok"), self)
        except Exception:  # noqa: BLE001 - whatever the candidate sent that does not decode
            # A malformed reply is the candidate breaking the protocol: the session is over, and the
            # test sees candidate_protocol, never a TypeError or ValueError it could take as expected.
            self._dead = "candidate_protocol"
            self.close()
            raise CandidateError(self._dead) from None
        raise error

    def mirror(self, value: object) -> int:
        """The shared id of a builtin container the test hands to candidate code."""
        key = id(value)
        if key not in self._mirror_ids:
            handle = len(self._mirror_ids) + 1
            self._mirror_ids[key] = handle
            self._mirrors[handle] = value
        return self._mirror_ids[key]

    def _outgoing(self) -> list[object]:
        """Every shared container whose state changed since it last crossed: the test's changes are
        in the candidate before any candidate code runs. Encoding may share new nested containers."""
        sync: list[object] = []
        done: set[int] = set()
        total = 0
        changed: list[tuple[int, str, list[object]]] = []
        while len(done) < len(self._mirrors):
            for handle in [key for key in self._mirrors if key not in done]:
                done.add(handle)
                state = _encode_data(self._mirrors[handle], 0, _encode_shared_item)
                text = json.dumps(state)
                # Every shared container is encoded on every request, changed or not: all of them count.
                total += len(text)
                if total > _MAX_SHARED_BYTES:
                    self._dead = _SHARED_TOO_LARGE
                    self.close()
                    raise CandidateError(self._dead)
                if self._synced.get(handle) != text:
                    changed.append((handle, text, state))
        for handle, text, state in changed:
            self._synced[handle] = text
            sync.append([handle, state])
        return sync

    def _incoming(self, sync: object) -> None:
        """Apply the candidate's container states to the test's own objects, in place."""
        if not isinstance(sync, list):
            raise CandidateError("candidate_protocol")
        states: list[tuple[object, list[object]]] = []
        for entry in sync:
            if not (isinstance(entry, list) and len(entry) == 2 and type(entry[0]) is int and isinstance(entry[1], list)):
                raise CandidateError("candidate_protocol")
            handle, state = entry
            kind = _MIRROR_KINDS.get(state[0] if state else None)
            if kind is None:
                raise CandidateError("candidate_protocol")
            existing = self._mirrors.get(handle)
            if existing is None:
                if handle >= 0:
                    raise CandidateError("candidate_protocol")
                existing = kind()
                self._mirrors[handle] = existing
                self._mirror_ids[id(existing)] = handle
            elif type(existing) is not kind:
                raise CandidateError("candidate_protocol")
            self._synced[handle] = json.dumps(state)
            states.append((existing, state))
        for target, state in states:
            _fill(target, _decode(state, self))

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
    if len(rest) != 1 and tag not in {"h", "v"}:
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
        handle, kind = rest[0], rest[1][:200]
        return session.remote(handle, lambda: _Remote(session, handle, kind))
    if tag == "v":
        return _decode_data_handle(rest, session, depth)
    if tag == "m" and type(value) is int and value in session._mirrors:
        return session._mirrors[value]
    raise CandidateError("candidate_protocol")


def _decode_data_handle(rest: list[object], session: _Session, depth: int) -> object:
    """["v", handle, qualname, base, snapshot, hooks, rebuild]: see _RemoteData."""
    if not (
        len(rest) == 6
        and type(rest[0]) is int
        and type(rest[1]) is str
        and rest[2] in _DATA_BASES
        and isinstance(rest[4], list)
        and all(hook in _DATA_HOOKS for hook in rest[4])
        and (rest[5] == "" or rest[5] in _STDLIB_KINDS)
    ):
        raise CandidateError("candidate_protocol")
    handle, kind, base, hooks, rebuild = rest[0], rest[1][:200], rest[2], frozenset(rest[4]), rest[5]
    snapshot = None
    if base in _IMMUTABLE_BASES:
        # Immutable: the data cannot change, so it is read once, in the test process.
        snapshot = _decode(rest[3], session, depth + 1)
        if type(snapshot) is not _DATA_BASES[base]:
            raise CandidateError("candidate_protocol")
    elif rest[3] is not None:
        raise CandidateError("candidate_protocol")
    return session.remote(handle, lambda: _RemoteData(session, handle, kind, base, snapshot, hooks, rebuild))


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
    if type(value) in _MIRRORED:
        return ["m", _session().mirror(value)]
    return _encode_data(value, depth, lambda item, level: _encode_trusted(item, level))


# Builtin containers that cross by reference, as in-process (Codex :512): each side keeps its own
# object under a shared id and the states are exchanged on every request and reply, so in-place
# changes, references kept by either side and identity behave as in one process. Candidate code only
# runs while a request is open, so the test never observes a state in between. A candidate's instance of
# a subclass (defaultdict, Counter, `class Result(list)`, a namedtuple, ...) crosses as a _RemoteData
# handle; a subclass instance the test passes in still crosses by value, as its builtin base.
_MIRRORED = (list, dict, set, bytearray)
# Shared containers are re-encoded on every request (there is no cheap way to tell an untouched builtin
# container), so their encoded total is capped like the candidate files: past it the session fails
# closed with shared_containers_too_large, before anything reaches the candidate.
_MAX_SHARED_BYTES = 8 * 1024 * 1024
_SHARED_TOO_LARGE = "shared_containers_too_large"
_MIRROR_KINDS: dict[object, type] = {"l": list, "d": dict, "S": set, "y": bytearray}

# Builtin data types a candidate class can subclass (Codex :1504), each with a C method of the base
# type that ignores the subclass's overrides and returns the exact base type.
_BASE_COPIES: dict[type, Any] = {
    int: int.__int__,
    float: float.__float__,
    complex: complex.__complex__,
    str: str.__str__,
    bytes: bytes.__bytes__,
    tuple: lambda value: tuple.__getitem__(value, slice(None)),
    frozenset: frozenset.copy,
    list: list.copy,
    dict: dict.copy,
    set: set.copy,
    bytearray: bytearray.copy,
}
_DATA_BASES: dict[str, type] = {kind.__name__: kind for kind in _BASE_COPIES}
_IMMUTABLE_BASES = frozenset({"int", "float", "complex", "str", "bytes", "tuple", "frozenset"})
_EXACT_DATA = (bool, int, float, complex, str, bytes, tuple, frozenset)
# The hooks that decide comparison, truth and hash. A candidate class that defines one itself (before
# its builtin base in the MRO) has that use refused, as for any candidate object.
_DATA_HOOKS = ("__eq__", "__ne__", "__lt__", "__le__", "__gt__", "__ge__", "__hash__", "__bool__", "__len__")
# Stdlib classes with their own comparison: the test rebuilds them from the data with its own copy of
# the class (the candidate's copy is never trusted) and compares that.
_STDLIB_KINDS: dict[str, type] = {"collections.Counter": collections.Counter, "collections.OrderedDict": collections.OrderedDict}


def _data_base(kind: type) -> type | None:
    return next((cls for cls in kind.__mro__ if cls in _BASE_COPIES), None)


def _data_hooks(kind: type, base: type) -> tuple[list[str], str]:
    """Candidate side: the hooks kind defines over its builtin base, and the stdlib class to rebuild."""
    hooks: list[str] = []
    rebuild = ""
    for cls in kind.__mro__:
        if cls is base:
            break
        name = f"{cls.__module__}.{cls.__qualname__}"
        if _STDLIB_KINDS.get(name) is cls:
            rebuild = rebuild or name
            continue
        hooks.extend(hook for hook in _DATA_HOOKS if hook in cls.__dict__ and hook not in hooks)
    return hooks, rebuild


def _encode_shared_item(item: object, depth: int) -> list[object]:
    encoded = _encode_trusted(item, depth)
    if not encoded:
        raise _unsupported("callable_passed_to_candidate" if callable(item) else "object_passed_to_candidate")
    return encoded


def _fill(target: object, value: object) -> None:
    """Make target (a shared container) hold value, keeping target's identity."""
    if (isinstance(target, list) and isinstance(value, list)) or (
        isinstance(target, bytearray) and isinstance(value, (bytes, bytearray))
    ):
        target[:] = value
    elif (isinstance(target, dict) and isinstance(value, dict)) or (isinstance(target, set) and isinstance(value, set)):
        target.clear()
        target.update(value)
    else:
        raise CandidateError("candidate_protocol")


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


class _Spawn:
    # True only while _Session._start starts the candidate process: the one process a test process
    # may start. Everything else that would start one is refused (threat model I-16).
    allowed = False


# Audit events that start a process (os.spawn* and pty.spawn go through os.fork / os.forkpty).
_SPAWN_EVENTS = frozenset({"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.fork", "os.forkpty"})
# libc symbols that start a process (or anything, for syscall) when called through ctypes.
_SPAWN_SYMBOLS = frozenset(
    {
        "fork", "vfork", "clone", "clone3", "system", "popen", "syscall", "posix_spawn", "posix_spawnp",
        "execve", "execv", "execvp", "execvpe", "execl", "execlp", "execle", "fexecve",
    }
)


def _spawn_hook(event: str, args: tuple[object, ...]) -> None:
    if event in _SPAWN_EVENTS:
        refused = True
    elif event == "ctypes.dlsym":
        name = args[1] if len(args) > 1 else None
        refused = isinstance(name, (str, bytes)) and (name if isinstance(name, str) else name.decode("ascii", "replace")) in _SPAWN_SYMBOLS
    else:
        return
    if refused and not _Spawn.allowed:
        raise _unsupported("process_spawn")


def _refuse_processes() -> None:
    """Threat model I-16: from here this test process starts no process but its candidate process.

    A child of a trusted test (subprocess, os.system, fork, posix_spawn, exec, multiprocessing, or
    libc's through ctypes) would run outside the candidate's Landlock sandbox with this process's view
    of the grading workspace. Each is refused with CandidateObjectUnsupported("process_spawn"), which
    fails the test whatever the test does with it. multiprocessing starts its spawn and forkserver
    children with _posixsubprocess.fork_exec, which raises no audit event: that entry is wrapped too.
    """
    import _posixsubprocess

    original = _posixsubprocess.fork_exec

    def fork_exec(*args: object, **kwargs: object) -> int:
        if not _Spawn.allowed:
            raise _unsupported("process_spawn")
        return original(*args, **kwargs)

    _posixsubprocess.fork_exec = fork_exec
    sys.addaudithook(_spawn_hook)


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

    def _operate(self, name: str, *operands: object) -> object:
        return self._aether("operate", name=name, args=[_encode_tree(item) for item in operands])

    def __delitem__(self, key: object) -> None:
        self._operate("delitem", self, key)

    def __next__(self) -> object:
        return self._operate("next", self)

    def __reversed__(self):
        value = self._operate("reversed", self)
        if type(value) is not list:
            raise TypeError("candidate reversal did not produce a list")
        return iter(value)

    def __format__(self, spec: str) -> str:
        return self._operate("format", self, spec)  # type: ignore[return-value]

    def __round__(self, digits: object = None) -> object:
        return self._operate("round", self) if digits is None else self._operate("round", self, digits)

    def __enter__(self) -> object:
        return self._operate("enter", self)

    def __exit__(self, kind: object, error: object, trace: object) -> bool:
        if kind is not None:
            # The exception cannot cross into the candidate, whose __exit__ would decide on it.
            raise _unsupported("exception_into_candidate_context")
        value = self._operate("exit", self)
        if value is None or type(value) in (bool, int):
            return bool(value)
        raise _unsupported("context_exit_result")

    def _unsupported_protocol(self, *_args: object) -> object:
        raise _unsupported("protocol_of_candidate_object")

    # No async protocol and no copying or pickling of a candidate object: these fail the test as
    # unsupported rather than raising a TypeError (or copying the proxy) that a test could accept.
    __await__ = __aiter__ = __anext__ = __aenter__ = __aexit__ = _unsupported_protocol  # type: ignore[assignment]
    __copy__ = __deepcopy__ = __reduce__ = __reduce_ex__ = __getstate__ = _unsupported_protocol  # type: ignore[assignment]


# Operations a candidate object answers for itself (Codex :627). Anything the proxy does not carry
# fails the test as unsupported (above), never as Python's own TypeError on the proxy.
_BINARY = ("add", "sub", "mul", "truediv", "floordiv", "mod", "pow", "matmul", "lshift", "rshift", "and", "or", "xor")
class _RemoteData(_Remote):
    """A candidate's instance of a builtin-type subclass (Codex :1504): `class Result(list)`, a
    namedtuple, an IntEnum, a str subclass, Counter, ... It is a handle like any candidate object, so
    attributes, methods and its own hooks (`__len__`, `__iter__`, `__missing__`, ...) run in the
    candidate as in-process. Comparison, truth and hash are what in-process Python would use when the
    class does not define them: those of its builtin data, computed here on data alone. A class that
    defines one of them gets that use refused (ADR-F44), so it cannot make `result == expected` pass.
    """

    __slots__ = ("_aether_base", "_aether_hooks", "_aether_rebuild", "_aether_snapshot")

    def __init__(
        self, session: _Session, handle: int, kind: str, base: str, snapshot: object, hooks: frozenset[str], rebuild: str
    ) -> None:
        super().__init__(session, handle, kind)
        object.__setattr__(self, "_aether_base", base)
        object.__setattr__(self, "_aether_snapshot", snapshot)
        object.__setattr__(self, "_aether_hooks", hooks)
        object.__setattr__(self, "_aether_rebuild", rebuild)

    def _aether_data(self, hook: str) -> object:
        if hook in self._aether_hooks:
            raise _unsupported("compare_candidate_object")
        base = _DATA_BASES[self._aether_base]
        if self._aether_base in _IMMUTABLE_BASES:
            value = self._aether_snapshot
        else:
            # Mutable: read now, so changes made through candidate methods are seen.
            value = self._aether("base")
            if base is bytearray and type(value) is bytes:
                value = bytearray(value)
            if type(value) is not base:
                raise CandidateError("candidate_protocol")
        rebuild = _STDLIB_KINDS.get(self._aether_rebuild)
        return value if rebuild is None else rebuild(value)

    def __eq__(self, other: object) -> bool:  # type: ignore[override]
        return self._aether_data("__eq__") == other

    def __ne__(self, other: object) -> bool:  # type: ignore[override]
        return self._aether_data("__ne__") != other

    def __lt__(self, other: object) -> bool:  # type: ignore[override]
        return self._aether_data("__lt__") < other  # type: ignore[operator]

    def __le__(self, other: object) -> bool:  # type: ignore[override]
        return self._aether_data("__le__") <= other  # type: ignore[operator]

    def __gt__(self, other: object) -> bool:  # type: ignore[override]
        return self._aether_data("__gt__") > other  # type: ignore[operator]

    def __ge__(self, other: object) -> bool:  # type: ignore[override]
        return self._aether_data("__ge__") >= other  # type: ignore[operator]

    def __bool__(self) -> bool:
        numeric = self._aether_base in {"int", "float", "complex"}
        if "__bool__" in self._aether_hooks or ("__len__" in self._aether_hooks and not numeric):
            raise _unsupported("truth_of_candidate_object")
        return bool(self._aether_data(""))

    def __hash__(self) -> int:
        if "__hash__" in self._aether_hooks:
            return object.__hash__(self)
        if self._aether_base not in _IMMUTABLE_BASES:
            raise TypeError(f"unhashable type: '{self._aether_type}'")
        return hash(self._aether_snapshot)


_OPERATIONS: dict[str, Any] = {
    **{name: getattr(operator, name + "_" if name in {"and", "or"} else name) for name in _BINARY},
    **{"i" + name: getattr(operator, "i" + name) for name in _BINARY},
    "divmod": divmod,
    "neg": operator.neg,
    "pos": operator.pos,
    "abs": operator.abs,
    "invert": operator.invert,
    "index": operator.index,
    "int": int,
    "float": float,
    "complex": complex,
    "bytes": bytes,
    "round": round,
    "trunc": math.trunc,
    "floor": math.floor,
    "ceil": math.ceil,
    "fspath": os.fspath,
    "next": next,
    "reversed": lambda value: list(reversed(value)),
    "format": format,
    "delitem": operator.delitem,
    "enter": lambda value: type(value).__enter__(value),
    "exit": lambda value: type(value).__exit__(value, None, None, None),
}


def _forward(name: str, reflected: bool = False):
    if reflected:
        return lambda self, other: self._operate(name, other, self)
    return lambda self, *operands: self._operate(name, self, *operands)


# The special methods Python looks up on the type: __add__ / __radd__ / __iadd__ ... for each binary
# operation, divmod, then the unary operations and conversions under their own names.
for _name in _BINARY:
    setattr(_Remote, f"__{_name}__", _forward(_name))
    setattr(_Remote, f"__r{_name}__", _forward(_name, reflected=True))
    setattr(_Remote, f"__i{_name}__", _forward("i" + _name))
for _name in ("divmod",):
    setattr(_Remote, f"__{_name}__", _forward(_name))
    setattr(_Remote, f"__r{_name}__", _forward(_name, reflected=True))
for _name in ("neg", "pos", "abs", "invert", "index", "int", "float", "complex", "bytes", "trunc", "floor", "ceil", "fspath"):
    setattr(_Remote, f"__{_name}__", _forward(_name))
del _name


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
# Candidate (non-grading) files copied into each candidate directory. Past this the run fails closed
# (candidate_files_too_large, the names logged): grading a tree with files left out would grade
# something other than the published patch (Codex :819). Copied once per step, so the bound keeps the copy far inside the protocol budget:
# a full 8 MiB (1000 files) onto the sandbox's tmpfs measured ~30 ms per step, ~3 s over the ~105
# steps the 240 s cap allows (each step is allowed 2 s), and it counts once against container memory. Source trees are far smaller; data sets that are
# not tests do not belong in a graded workspace.
_MAX_CANDIDATE_BYTES = 8 * 1024 * 1024
_DROPPED_NAMES_LOGGED = 20
_CANDIDATE_FILES_TOO_LARGE = "candidate_files_too_large"


def _candidate_files(root: Path) -> tuple[dict[str, bytes], list[str]]:
    """The workspace files candidate code may see: regular, non-grading files outside skipped dirs,
    and the names of those that did not fit under _MAX_CANDIDATE_BYTES (the run then fails closed).

    Read once, before any candidate code runs, and written into a fresh directory for every test process.
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
            if not path.is_file() or _is_grading(name):
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
        sys.stderr.write(f"aether-protocol: candidate_files_too_large count={len(dropped)} limit_bytes={_MAX_CANDIDATE_BYTES} names=[{names}]{more}\n")
        sys.stderr.flush()
    return found, dropped


def _candidate_sandbox(files: dict[str, bytes], deny: list[str], shm: str) -> dict[str, Any]:
    """A fresh directory for one test process's candidate process: src/ (its cwd, the candidate files) and tmp/."""
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


# One reply per finished test; past this a test process is talking nonsense and is stopped.
_MAX_REPLIES = 100_000


def _run_in_child(
    root: Path,
    request: dict[str, object],
    step_timeout: float,
    deadline: float,
    candidate: tuple[dict[str, bytes], list[str], str],
) -> tuple[list[dict[str, object]], str]:
    """Run one step (a pytest session over one test module) in a fresh test process.

    Returns the replies it sent, in order, and how it ended: "done", "restart" (its candidate process
    died; the remaining tests need a fresh one), "timeout" (no reply within `step_timeout` of the last,
    or the run's deadline passed) or "no_result" (a crash, an early exit, a malformed message).

    Replies arrive on a pipe only that process holds, never on stdout. The test process runs trusted
    test code only; candidate code runs in its own process behind it, in a fresh candidate directory
    (`candidate` = its files, the paths it must not read and the shared memory) removed after the step.
    """
    try:
        sandbox = _candidate_sandbox(*candidate)
    except OSError:
        return [], "no_result"
    try:
        return _run_test_process(root, {**request, "sandbox": sandbox}, step_timeout, deadline)
    finally:
        _remove_tree(sandbox["top"])


def _run_test_process(
    root: Path, request: dict[str, object], step_timeout: float, deadline: float
) -> tuple[list[dict[str, object]], str]:
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
        return [], "no_result"
    _LIVE.add(process.pid)
    os.close(request_read)
    os.close(reply_write)
    replies: list[dict[str, object]] = []
    ending = "no_result"
    try:
        _send(request_write, request)
        os.close(request_write)
        request_write = -1
        while len(replies) < _MAX_REPLIES:
            # Every test gets its own limit, counted from the previous reply; the run's deadline caps it.
            reply = _receive(reply_read, min(time.monotonic() + step_timeout, deadline))
            if not isinstance(reply, dict):
                break
            kind = reply.get("kind")
            if kind in {"done", "restart"}:
                ending = str(kind)
                break
            replies.append(reply)
            if kind == "import_failed":
                ending = "done"
                break
    except TimeoutError:
        ending = "timeout"
    except (_ChannelClosed, OSError, ValueError):
        ending = "no_result"
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
    return replies, ending


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


def _declared_found(name: str, found: list[str]) -> bool:
    """A declared id pytest collected: the id itself, or its parametrized instances (`test_x[1]`)."""
    return any(item == name or item.startswith(name + "[") for item in found)


def _under(name: str, owners: dict[str, str]) -> bool:
    return any(name == owner or name.startswith(owner + ".") for owner in owners)


def _grade_module(
    root: Path,
    request: dict[str, object],
    declared: list[str],
    unsupported: dict[str, str],
    test_timeout: float,
    deadline: float,
    candidate: tuple[dict[str, bytes], list[str], str],
    tampered,
) -> list[tuple[str, str, str]] | None:
    """(test name, verdict, detail) for one test module, or None when grading files were tampered with.

    One test process runs a pytest session over the module and replies per test. If it ends early (a
    crash, a timeout, or its candidate process died), the test that was running fails and a fresh test
    process, with a fresh candidate directory, runs the tests that have not reported yet.
    """
    order: list[str] | None = None
    unsupported = dict(unsupported)
    results: dict[str, tuple[str, str]] = {}
    module_failure: tuple[str, str] | None = None
    while True:
        if tampered():
            return None
        if deadline - time.monotonic() <= 0:
            break
        replies, ending = _run_in_child(
            root,
            {**request, "skip": sorted(results), "exclude": sorted(unsupported)},
            test_timeout,
            deadline,
            candidate,
        )
        progressed = False
        for reply in replies:
            kind = reply.get("kind")
            if kind == "import_failed":
                module_failure = ("FAIL", "import:" + str(reply.get("detail", ""))[:200])
            elif kind == "collected" and order is None:
                found, found_unsupported = _probe_ids(reply)
                unsupported = {**found_unsupported, **unsupported}
                order = [name for name in found if not _under(name, unsupported)]
                if reply.get("error"):
                    module_failure = ("FAIL", "collect:" + str(reply.get("error"))[:200])
            elif kind == "result" and order is not None:
                name = reply.get("name")
                verdict = reply.get("verdict")
                if type(name) is str and name in order and name not in results and verdict in {"PASS", "FAIL"}:
                    results[name] = (str(verdict), "" if verdict == "PASS" else str(reply.get("detail", ""))[:200] or "no_result")
                    progressed = True
        if order is None:
            # The module never got as far as its tests.
            if module_failure is None:
                module_failure = ("FAIL", "import:" + ("timeout" if ending == "timeout" else "no_result"))
            break
        if module_failure is not None and module_failure[1].startswith("import:"):
            break
        pending = [name for name in order if name not in results]
        if ending == "done" or not pending:
            for name in pending:
                # The session finished without reporting it.
                results[name] = ("FAIL", "no_result")
            break
        if ending == "restart" and progressed:
            continue
        # The test that was running when the process ended is the first that has not reported.
        results[pending[0]] = ("FAIL", "timeout" if ending == "timeout" else "no_result")
    graded: list[tuple[str, str, str]] = []
    if module_failure is not None:
        graded.append(("", *module_failure))
        if order is None or module_failure[1].startswith("import:"):
            # The module itself failed: its tests never ran, and are not listed one by one.
            return graded
    for name in sorted(unsupported):
        graded.append((name, "FAIL", unsupported[name]))
    found = order or []
    for name in sorted(set(declared) - set(found)):
        if _under(name, unsupported) or _declared_found(name, found):
            continue
        # A test the baseline source declares that pytest did not collect: gone, or no longer a test.
        graded.append((name, "FAIL", "not_a_test"))
    for name in sorted(found):
        verdict, detail = results.get(name, ("FAIL", "timeout"))
        graded.append((name, verdict, detail))
    return graded


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
    # First: it also sets _MARKED, which decides what is grading for everything below.
    baseline_modules = _baseline_modules()
    files = _files(root)
    sources = {name: path.read_bytes() for name, path in files.items()}
    baseline = {name: hashlib.sha256(data).hexdigest() for name, data in sources.items()}
    tests = _test_modules(sources)
    encoded = {name: base64.b64encode(data).decode("ascii") for name, data in sources.items()}
    # What candidate code may see, read before any of it runs; and what it must never read.
    candidate_files, over_cap = _candidate_files(root)
    if over_cap:
        # Before any test or candidate process exists: nothing is graded against a partial tree.
        return {"passed": 0, "failed": 1, "errors": 0, "cases": [_case("suite", "FAIL", _CANDIDATE_FILES_TOO_LARGE)], "elapsed_seconds": 0.0}
    candidate = (candidate_files, [str(root), os.environ.get("AETHER_SANDBOX_WORK", "/work")], shared_memory)
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
            "marked": sorted(_MARKED),
        }
        outcome = _grade_module(root, request, declared, unsupported, test_timeout, deadline, candidate, lambda: _tampered(root, baseline))
        if outcome is None:
            tampered = True
            break
        for name, verdict, detail in outcome:
            if verdict == "PASS":
                passed += 1
            else:
                failed += 1
            cases.append(_case(label + ("." + name if name else ""), verdict, detail))
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


def _trusted_class(value: object) -> bool:
    return isinstance(value, type) and not issubclass(value, (_Remote, _RemoteModule))


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


def _init_classes(module: types.ModuleType) -> dict[str, str]:
    """Test classes this module defines that pytest refuses to collect (an `__init__`): counted failures.

    One the module merely imports (e.g. a test client) is not a test."""
    found: dict[str, str] = {}
    for attr, value in vars(module).items():
        if not attr.startswith("Test") or not _trusted_class(value) or issubclass(value, unittest.TestCase):
            continue
        if getattr(value, "__test__", True) is False or getattr(value, "__module__", None) != module.__name__:
            continue
        if "__init__" in {name for klass in value.__mro__[:-1] for name in vars(klass)}:
            found[attr] = "unsupported_test_class:init"
    return found


def _kind_detail(item: object) -> str:
    """FAIL detail for a test pytest would not really run: async functions and generators."""
    cls = getattr(item, "cls", None)
    name = getattr(item, "originalname", None) or getattr(item, "name", "")
    if isinstance(cls, type) and issubclass(cls, unittest.TestCase):
        fn = getattr(cls, name, None)
        if issubclass(cls, unittest.IsolatedAsyncioTestCase):
            return "unsupported_generator" if inspect.isgeneratorfunction(fn) else ""
    else:
        fn = getattr(item, "obj", None)
    if inspect.iscoroutinefunction(fn):
        return "async"
    if inspect.isasyncgenfunction(fn):
        return "unsupported_async_generator"
    if inspect.isgeneratorfunction(fn):
        return "unsupported_generator"
    return ""


def _item_name(item: object) -> str:
    """`test_x`, `TestClass.test_m` or `Case.test_m[param]`: the pytest node id without its file."""
    return ".".join(str(getattr(item, "nodeid", "")).split("::")[1:])


def _pytest_detail(exc: BaseException) -> str:
    message = str(exc)
    kind = type(exc).__name__
    if kind == "Failed" and message.startswith("Unexpected success"):
        return "unexpected_success"
    if kind == "Failed" and "async def functions are not natively supported" in message:
        return "async"
    return _failure_detail(exc)


def _pytest_arguments(root: Path, target: Path) -> list[str]:
    """pytest reads nothing from the workspace: no config file (-c /dev/null), no conftest, no cache;
    plugins only from this list (autoload is off); the module object comes from the snapshot."""
    return [
        "-q",
        "-s",
        "--no-header",
        "--tb=native",
        "--assert=plain",
        "--noconftest",
        "-c",
        os.devnull,
        "--rootdir",
        str(root),
        "-p",
        "no:cacheprovider",
        str(target),
    ]


def _run_pytest(root: Path, relative: str, module: types.ModuleType, request: dict[str, Any], emit) -> str:
    """Run one test module's tests with pytest in this test process; "done" or "restart".

    Replies go through `emit`: the collected test names, then one result per test as it finishes.
    "restart" means the candidate process died during a test: the grader resumes the remaining tests
    in a fresh test process (and candidate directory), so one crash fails only the test it happened in.
    """
    for key in [key for key in os.environ if key.upper().startswith("PYTEST_")]:
        del os.environ[key]  # PYTEST_ADDOPTS, PYTEST_PLUGINS, ...
    os.environ["PYTEST_DISABLE_PLUGIN_AUTOLOAD"] = "1"
    import pytest  # the sandbox interpreter's own: `pytest` is a shadow root, never a workspace module

    target = root / relative
    skip = set(request.get("skip") or ())
    exclude = {str(name): "" for name in request.get("exclude") or ()}

    class _SnapshotModule(pytest.Module):
        """The test module already imported from the snapshot; pytest never imports the file."""

        def _getobj(self):
            return module

    class _InertModule(pytest.Module):
        """Any other file pytest sees while walking to the target: never imported, holds nothing."""

        def _getobj(self):
            return types.ModuleType("aether_inert")

        def collect(self):
            return []

    class _Bridge:
        def __init__(self) -> None:
            self.ending = "done"
            self.session: Any = None
            self.items: dict[str, object] = {}
            self.outcomes: dict[str, tuple[str, str]] = {}
            self.collect_error = ""

        def pytest_sessionstart(self, session) -> None:
            self.session = session

        @pytest.hookimpl(tryfirst=True)
        def pytest_collect_directory(self, path, parent):
            # A plain directory, never a Package: pytest would import the package's __init__.py
            # (which may be candidate code) in this process.
            return pytest.Dir.from_parent(parent, path=path)

        @pytest.hookimpl(tryfirst=True)
        def pytest_pycollect_makemodule(self, module_path, parent):
            if Path(module_path) == target:
                return _SnapshotModule.from_parent(parent, path=module_path)
            return _InertModule.from_parent(parent, path=module_path)

        @pytest.hookimpl(tryfirst=True)
        def pytest_pycollect_makeitem(self, collector, name, obj):
            # A candidate object is never a test, and is not probed (that would run candidate code).
            if isinstance(obj, (_Remote, _RemoteModule)):
                return []
            if (inspect.isgeneratorfunction(obj) or inspect.isasyncgenfunction(obj)) and collector.funcnamefilter(name):
                # pytest makes a generator test a collection error, which stops the whole module; it is
                # an item here, failed as unsupported_(async_)generator (calling it runs none of its body).
                return [pytest.Function.from_parent(collector, name=name)]
            return None

        def pytest_collectreport(self, report) -> None:
            if report.failed and not self.collect_error:
                self.collect_error = str(report.longrepr).strip().splitlines()[-1][:200] if report.longrepr else "collect_error"

        @pytest.hookimpl(trylast=True)
        def pytest_collection_modifyitems(self, session, config, items) -> None:
            names = [_item_name(item) for item in items]
            emit({"kind": "collected", "names": names, "unsupported": _init_classes(module), "error": self.collect_error})
            keep, dropped = [], []
            for item, name in zip(items, names, strict=True):
                if name in skip or _under(name, exclude):
                    dropped.append(item)
                else:
                    keep.append(item)
                    self.items[item.nodeid] = item
            if dropped:
                config.hook.pytest_deselected(items=dropped)
                items[:] = keep

        def pytest_runtest_logstart(self, nodeid, location) -> None:
            _Current.unsupported = None

        @pytest.hookimpl(wrapper=True)
        def pytest_runtest_makereport(self, item, call):
            report = yield
            if call.excinfo is not None and report.failed:
                report._aether_detail = _pytest_detail(call.excinfo.value)
            return report

        def pytest_runtest_logreport(self, report) -> None:
            if report.nodeid in self.outcomes and self.outcomes[report.nodeid][0] == "FAIL":
                return  # the first failure of a test is its verdict
            if report.failed:
                detail = getattr(report, "_aether_detail", "")
                if not detail and "XPASS" in str(report.longrepr):
                    detail = "unexpected_success"
                self.outcomes[report.nodeid] = ("FAIL", detail or "failed")
            elif report.skipped:
                self.outcomes[report.nodeid] = ("FAIL", "expected_failure" if hasattr(report, "wasxfail") else "skipped")
            elif report.passed and hasattr(report, "wasxfail"):
                self.outcomes[report.nodeid] = ("FAIL", "unexpected_success")
            elif report.passed and report.when == "call":
                self.outcomes.setdefault(report.nodeid, ("PASS", ""))

        def pytest_runtest_logfinish(self, nodeid, location) -> None:
            item = self.items.get(nodeid)
            verdict, detail = self.outcomes.pop(nodeid, ("FAIL", "no_result"))
            kind = _kind_detail(item) if item is not None else ""
            if kind:
                verdict, detail = "FAIL", kind
            elif _Current.unsupported is not None:
                # Any unsupported use of a candidate object fails the test, even if the test caught it.
                verdict, detail = "FAIL", "candidate_object_unsupported:" + _Current.unsupported
            emit({"kind": "result", "name": _item_name(item) if item is not None else "", "verdict": verdict, "detail": detail})
            current = _Current.session
            if current is not None and current._dead is not None and self.session is not None:
                # Its candidate process is gone: the rest of the module runs against a fresh one.
                self.ending = "restart"
                self.session.shouldstop = "aether: candidate process ended"

    bridge = _Bridge()
    pytest.main(_pytest_arguments(root, target), plugins=[bridge])
    return bridge.ending


def _runner_step(root: Path, request: dict[str, Any], emit) -> str:
    global _MARKED
    _MARKED = frozenset(str(name) for name in request.get("marked") or ())
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
    # Before any test code runs, its import included.
    _refuse_processes()
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
            emit({"kind": "import_failed", "detail": _failure_detail(exc)})
            return "done"
        return _run_pytest(root, relative, module, request, emit)
    finally:
        _Current.session.close()
        _Current.session = None


def _runner_main(read_fd: int, write_fd: int, parent: int) -> None:
    _die_with_parent(parent)
    _not_dumpable()
    sys.dont_write_bytecode = True
    # The report pipe stays out of anything started from here (threat model I-16); pass_fds made it
    # inheritable for this process only.
    os.set_inheritable(write_fd, False)
    request = _receive(read_fd)
    os.close(read_fd)
    if not isinstance(request, dict):
        os._exit(1)

    def emit(message: dict[str, object]) -> None:
        _send(write_fd, message)

    try:
        ending = _runner_step(Path.cwd(), request, emit)
    except BaseException:  # noqa: BLE001 - no reply is a FAIL in the grader
        os._exit(1)
    _send(write_fd, {"kind": ending})
    os.close(write_fd)


# ---------------------------------------------------------------------------------------------
# Candidate side. Runs candidate code; everything it says is treated as untrusted data.


def _candidate_main(read_fd: int, write_fd: int) -> None:
    handles: dict[int, object] = {}
    # Shared builtin containers (see _MIRRORED): the candidate's own objects under the shared ids.
    mirrors: dict[int, object] = {}
    mirror_ids: dict[int, int] = {}
    synced: dict[int, str] = {}

    def mirror(value: object) -> list[object]:
        key = id(value)
        if key not in mirror_ids:
            shared = -(len(mirror_ids) + 1)
            mirror_ids[key] = shared
            mirrors[shared] = value
        return ["m", mirror_ids[key]]

    kept: dict[int, int] = {}

    def keep(value: object) -> list[object]:
        # One handle per object (handles keeps it alive, so its id stays its own).
        handle = kept.get(id(value))
        if handle is None:
            handle = len(handles) + 1
            handles[handle] = value
            kept[id(value)] = handle
        return ["h", handle, type(value).__qualname__]

    def encode(value: object, depth: int = 0) -> list[object]:
        if depth > _MAX_DEPTH:
            raise ValueError("value_too_deep")
        if isinstance(value, types.ModuleType):
            return keep(value)
        if type(value) in _MIRRORED:
            return mirror(value)
        if value is None or type(value) in _EXACT_DATA:
            return _encode_data(value, depth, encode)
        base = _data_base(type(value))
        if base is None:
            return keep(value)
        # A builtin-type subclass (Codex :1504): kept by reference, with its builtin data for comparison.
        held = keep(value)
        hooks, rebuild = _data_hooks(type(value), base)
        snapshot = _encode_data(_BASE_COPIES[base](value), depth, encode) if base.__name__ in _IMMUTABLE_BASES else None
        return ["v", held[1], held[2], base.__name__, snapshot, hooks, rebuild]

    def exact(value: object) -> object:
        # str/bytes from a conversion (str(), format(), os.fspath(), bytes()) are text, not a handle.
        if isinstance(value, str):
            return str.__str__(value)
        if isinstance(value, bytes):
            return bytes.__bytes__(value)
        return value

    def decode(node: object) -> object:
        if isinstance(node, list) and node and node[0] == "h":
            return handles[node[1]]
        if isinstance(node, list) and node and node[0] == "m":
            return mirrors[node[1]]
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
            return encode(exact(str(target)))
        if op == "base":
            base = _data_base(type(target))
            if base is None:
                raise ValueError("unknown_op")
            return _encode_data(_BASE_COPIES[base](target), 0, encode)
        if op == "operate":
            # One of a fixed set of operations, run here exactly as Python would run it in-process
            # (reflected and in-place forms included), so a TypeError is the candidate's own.
            name = message["name"]
            result = _OPERATIONS[name](*[decode(item) for item in message["args"]])
            return encode(exact(result) if name in {"format", "fspath", "bytes"} else result)
        raise ValueError("unknown_op")

    def receive_states(sync: list) -> None:
        """The test's container states, applied in place before any candidate code runs."""
        states = []
        for shared, state in sync:
            kind = _MIRROR_KINDS[state[0]]
            existing = mirrors.get(shared)
            if existing is None:
                if shared <= 0:
                    raise ValueError("candidate_protocol")
                existing = kind()
                mirrors[shared] = existing
                mirror_ids[id(existing)] = shared
            elif type(existing) is not kind:
                raise ValueError("candidate_protocol")
            synced[shared] = json.dumps(state)
            states.append((existing, state))
        for target, state in states:
            _fill_candidate(target, decode(state))

    def send_states() -> list[object]:
        sync: list[object] = []
        done: set[int] = set()
        while len(done) < len(mirrors):
            for shared in [key for key in mirrors if key not in done]:
                done.add(shared)
                state = _encode_data(mirrors[shared], 0, encode)
                text = json.dumps(state)
                if synced.get(shared) != text:
                    synced[shared] = text
                    sync.append([shared, state])
        return sync

    while True:
        try:
            message = _receive(read_fd)
        except _ChannelClosed:
            return
        try:
            receive_states(message.get("sync", []))
            reply: dict[str, object] = {"ok": handle(message)}  # type: ignore[arg-type]
        except BaseException as exc:  # noqa: BLE001 - every candidate failure is reported, even SystemExit
            reply = {"err": _candidate_error(exc)}
        try:
            # Sent with the result and with an exception alike: changes made before a raise are real.
            reply["sync"] = send_states()
        except BaseException as exc:  # noqa: BLE001 - a state that cannot be sent fails this request
            reply = {"err": _candidate_error(exc)}
        _send(write_fd, reply)


def _candidate_error(exc: BaseException) -> list[str]:
    base = next(
        (kind.__name__ for kind in type(exc).__mro__ if _BUILTIN_ERRORS.get(kind.__name__) is kind),
        "CandidateError",
    )
    return [base, type(exc).__qualname__, str(exc)[:2000]]


def _fill_candidate(target: object, value: object) -> None:
    if isinstance(target, dict):
        target.clear()
        target.update(value)  # type: ignore[call-overload]
    elif isinstance(target, set):
        target.clear()
        target.update(value)  # type: ignore[arg-type]
    else:
        target[:] = value  # type: ignore[index]


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
