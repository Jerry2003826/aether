"""Candidate code sees only candidate files: it cannot read tests or other grading files from disk.

Reported by Codex (coding_protocol.py:301): the candidate process ran with the restored grading
workspace as its cwd, so `open("test_secret.py")` read the expected values; the meta-path finder and
the hash checks only stop imports and changes. The candidate process now runs in a fresh directory
holding only candidate files, and the grading workspace is unreadable to it, by any path.
"""

from __future__ import annotations

from pathlib import Path

from tests.unit.test_r10_oracle_isolation import _assert_signed, _grade, _write

_SECRET = 731904

_APP = (
    "import os\n"
    "import re\n"
    "import tempfile\n"
    "\n"
    "def _read(path):\n"
    "    try:\n"
    "        with open(path, encoding='utf-8') as handle:\n"
    "            return handle.read()\n"
    "    except OSError:\n"
    "        return None\n"
    "\n"
    "def secret(path='test_secret.py'):\n"
    "    found = re.search(r'EXPECTED = (\\d+)', _read(path) or '')\n"
    "    return int(found.group(1)) if found else -1\n"
    "\n"
    "def via_parent():\n"
    "    # The test process's cwd is the grading workspace.\n"
    "    return secret(f'/proc/{os.getppid()}/cwd/test_secret.py')\n"
    "\n"
    "def listing(path):\n"
    "    try:\n"
    "        return sorted(os.listdir(path))\n"
    "    except OSError:\n"
    "        return None\n"
    "\n"
    "def data():\n"
    "    return _read('data.txt'), _read(os.path.join(os.path.dirname(__file__), 'data.txt'))\n"
    "\n"
    "def scratch():\n"
    "    with tempfile.NamedTemporaryFile('w+') as handle:\n"
    "        handle.write('ok')\n"
    "        handle.flush()\n"
    "        handle.seek(0)\n"
    "        return handle.read()\n"
)

_TESTS = (
    "from pathlib import Path\n"
    "\n"
    "import app\n"
    "\n"
    f"EXPECTED = {_SECRET}\n"
    "\n"
    "def test_relative():\n"
    "    assert app.secret() == EXPECTED\n"
    "\n"
    "def test_absolute():\n"
    "    assert app.secret(str(Path(__file__).resolve())) == EXPECTED\n"
    "\n"
    "def test_parent_cwd():\n"
    "    assert app.via_parent() == EXPECTED\n"
    "\n"
    "def test_reads_are_denied():\n"
    "    assert app.secret() == -1\n"
    "    assert app.secret(str(Path(__file__).resolve())) == -1\n"
    "    assert app.via_parent() == -1\n"
    "    assert app.listing(str(Path(__file__).resolve().parent)) is None\n"
    "\n"
    "def test_candidate_files_still_work():\n"
    "    assert app.data() == ('data\\n', 'data\\n')\n"
    "    assert app.scratch() == 'ok'\n"
    "    assert 'test_secret.py' not in app.listing('.')\n"
)

_LEAKS = ("test_secret.test_relative", "test_secret.test_absolute", "test_secret.test_parent_cwd")


def test_candidate_cannot_read_test_files_and_the_job_fails(tmp_path: Path) -> None:
    _write(tmp_path, {"app.py": _APP, "data.txt": "data\n", "test_secret.py": _TESTS})
    code, body, output = _grade(tmp_path)
    assert body is not None, output
    _assert_signed(body, output)
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    # Every way to read the expected value fails, so the job is graded FAIL ...
    assert code == 1, (code, body)
    for name in _LEAKS:
        assert verdicts.get(name) == "FAIL", (name, body)
    # ... because the reads themselves fail, not because something else broke.
    assert verdicts.get("test_secret.test_reads_are_denied") == "PASS", body
    assert verdicts.get("test_secret.test_candidate_files_still_work") == "PASS", body
    assert body["passed"] == 2 and body["failed"] == 3, body


def test_grading_support_files_are_not_readable_either(tmp_path: Path) -> None:
    _write(
        tmp_path,
        {
            "app.py": (
                "def value():\n"
                "    for path in ('oracle.py', 'conftest.py'):\n"
                "        try:\n"
                "            with open(path) as handle:\n"
                "                return handle.read()\n"
                "        except OSError:\n"
                "            pass\n"
                "    return None\n"
            ),
            "oracle.py": "ANSWER = 42\n",
            "conftest.py": "",
            "test_value.py": "import app\n\ndef test_value():\n    assert app.value() is None\n",
        },
    )
    code, body, output = _grade(tmp_path)
    assert body is not None, output
    assert code == 0, (code, body)
    assert body["passed"] == 1 and body["failed"] == 0, body


def test_without_the_filesystem_lock_no_candidate_code_runs(tmp_path: Path, monkeypatch) -> None:
    # A read path that would expose a denied one (here: "/work" is the interpreter's /usr) cannot be
    # locked safely, so the candidate refuses to start: fail closed, with an explicit detail.
    import tests.unit.test_r10_oracle_isolation as harness

    marker = tmp_path / "ran.txt"
    root = tmp_path / "job"
    root.mkdir()
    _write(
        root,
        {
            "app.py": f"open({str(marker)!r}, 'w').write('ran')\n\ndef value():\n    return 42\n",
            "test_value.py": "import app\n\ndef test_value():\n    assert app.value() == 42\n",
        },
    )
    real_run = harness.subprocess.run

    def run(*args, **kwargs):
        kwargs["env"] = {**kwargs["env"], "AETHER_SANDBOX_WORK": "/usr"}
        return real_run(*args, **kwargs)

    monkeypatch.setattr(harness.subprocess, "run", run)
    code, body, output = _grade(root)
    assert body is not None, output
    assert code == 1, (code, body)
    assert body["passed"] == 0, body
    assert {case["detail"] for case in body["cases"]} == {"import:candidate_isolation_unavailable"}, body
    assert not marker.exists()


_HELPER_APP = (
    "import os\n"
    "import re\n"
    "\n"
    "def secret(path):\n"
    "    try:\n"
    "        with open(path, encoding='utf-8') as handle:\n"
    "            found = re.search(r'EXPECTED = (\\d+)', handle.read())\n"
    "    except OSError:\n"
    "        return -1\n"
    "    return int(found.group(1)) if found else -1\n"
    "\n"
    "def via_parent():\n"
    "    return secret(f'/proc/{os.getppid()}/cwd/tests/check.py')\n"
    "\n"
    "def imported():\n"
    "    try:\n"
    "        import check\n"
    "    except ImportError:\n"
    "        return -1\n"
    "    return check.EXPECTED\n"
)
# A grading helper next to the tests, imported by bare name: grading (restored from the baseline by
# the entry, never served to candidate code), so it must not be readable either. (A helper at the
# workspace root is not grading: it is candidate code the agent may edit, and the candidate imports it.)
_HELPER_TESTS = (
    "import check\n"
    "import app\n"
    "\n"
    "def test_relative():\n"
    "    assert app.secret('tests/check.py') == check.EXPECTED\n"
    "\n"
    "def test_absolute():\n"
    "    assert app.secret(check.__file__) == check.EXPECTED\n"
    "\n"
    "def test_parent_cwd():\n"
    "    assert app.via_parent() == check.EXPECTED\n"
    "\n"
    "def test_import():\n"
    "    assert app.imported() == check.EXPECTED\n"
    "\n"
    "def test_helper_is_denied():\n"
    "    assert app.secret('tests/check.py') == -1\n"
    "    assert app.secret('check.py') == -1\n"
    "    assert app.secret(check.__file__) == -1\n"
    "    assert app.via_parent() == -1\n"
    "    assert app.imported() == -1\n"
)
_HELPER_FILES = {
    "app.py": _HELPER_APP,
    "tests/check.py": "EXPECTED = 58213\n",
    "tests/test_helper.py": _HELPER_TESTS,
}
_HELPER_LEAKS = tuple(f"tests.test_helper.{name}" for name in ("test_relative", "test_absolute", "test_parent_cwd", "test_import"))


def test_grading_helpers_next_to_tests_are_not_readable(tmp_path: Path) -> None:
    _write(tmp_path, _HELPER_FILES)
    code, body, output = _grade(tmp_path)
    assert body is not None, output
    verdicts = {case["name"]: case["verdict"] for case in body["cases"]}
    assert code == 1, body
    for name in _HELPER_LEAKS:
        assert verdicts.get(name) == "FAIL", (name, body)
    assert verdicts.get("tests.test_helper.test_helper_is_denied") == "PASS", body
