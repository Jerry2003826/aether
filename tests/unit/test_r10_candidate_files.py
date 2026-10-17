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
