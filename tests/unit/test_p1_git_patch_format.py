"""unified_patch writes git's own patch format, and git apply reproduces the candidate files exactly.

Each case really runs `git apply` on the baseline and compares every resulting file, byte for byte,
with the candidate files. Covered: a deleted file, a new file, an empty file created or deleted
(alone and mixed with other changes), non-UTF-8 bytes, a missing final newline, and a change only to
the final newline.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest
from aether_worker.coding_workspace import unified_patch


def _write_tree(root: Path, files: dict[str, bytes]) -> None:
    for name, data in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)


def _read_tree(root: Path) -> dict[str, bytes]:
    found: dict[str, bytes] = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if ".git" in relative.parts or not path.is_file():
            continue
        found[relative.as_posix()] = path.read_bytes()
    return found


def _git_apply(baseline: dict[str, bytes], patch: str, where: Path) -> dict[str, bytes]:
    """Apply patch with git itself to a fresh copy of baseline and return the resulting files."""
    where.mkdir()
    _write_tree(where, baseline)
    subprocess.run(["git", "init", "-q"], cwd=where, check=True, capture_output=True)
    patch_file = where.parent / f"{where.name}.patch"
    patch_file.write_bytes(patch.encode("utf-8"))
    applied = subprocess.run(
        ["git", "apply", "--verbose", "--whitespace=nowarn", str(patch_file)],
        cwd=where,
        capture_output=True,
        check=False,
    )
    assert applied.returncode == 0, (applied.stderr.decode(errors="replace"), patch)
    return _read_tree(where)


def _raw(files: dict[str, str | bytes]) -> dict[str, bytes]:
    return {name: data if isinstance(data, bytes) else data.encode("utf-8") for name, data in files.items()}


CASES: dict[str, tuple[dict[str, str | bytes], dict[str, str | bytes]]] = {
    "delete_file": ({"app.py": "x = 1\n", "gone.py": "G = 1\nH = 2\n"}, {"app.py": "x = 1\n"}),
    "new_file": ({"app.py": "x = 1\n"}, {"app.py": "x = 1\n", "pkg/mod.py": "NEW = 1\n"}),
    "create_empty_alone": ({"app.py": "x = 1\n"}, {"app.py": "x = 1\n", "pkg/__init__.py": ""}),
    "delete_empty_alone": ({"app.py": "x = 1\n", "legacy.py": ""}, {"app.py": "x = 1\n"}),
    "empty_files_mixed": (
        {"app.py": "x = 1\n", "legacy.py": "", "gone.py": "G = 1\n", "emptied.py": "E = 1\n"},
        {"app.py": "x = 2\n", "emptied.py": "", "pkg/__init__.py": "", "new.py": "N = 1\n"},
    ),
    "non_ascii_text": ({"app.py": "NAME = 'café'\n"}, {"app.py": "NAME = 'crème ✓'\n"}),
    "non_utf8_bytes": (
        {"app.py": "x = 1\n", "latin.py": b"NAME = 'caf\xe9'\n", "blob.py": b"\x00\x01\x02\xff"},
        {"app.py": "x = 1\n", "latin.py": b"NAME = 'cr\xe8me'\n", "blob.py": b"\x00\x01\x03\xfe", "fresh.py": b"\x89PNG\r\n\x1a\n\x00"},
    ),
    "missing_final_newline": ({"app.py": "A = 1\nB = 2"}, {"app.py": "A = 1\nB = 3"}),
    "only_final_newline_added": ({"app.py": "A = 1"}, {"app.py": "A = 1\n"}),
    "only_final_newline_removed": ({"app.py": "A = 1\n"}, {"app.py": "A = 1"}),
}


@pytest.mark.parametrize("case", sorted(CASES))
def test_git_apply_of_the_patch_reproduces_the_candidate_files_byte_for_byte(tmp_path: Path, case: str) -> None:
    base, files = CASES[case]
    patch = unified_patch(base, files)
    assert patch.startswith("diff --git "), patch
    assert _git_apply(_raw(base), patch, tmp_path / "applied") == _raw(files)


def test_a_deleted_file_is_gone_after_git_apply_not_left_empty(tmp_path: Path) -> None:
    base, files = CASES["delete_file"]
    patch = unified_patch(base, files)
    assert "deleted file mode" in patch and "+++ /dev/null" in patch, patch
    assert "gone.py" not in _git_apply(_raw(base), patch, tmp_path / "applied")


def test_an_emptied_file_stays_a_modification(tmp_path: Path) -> None:
    patch = unified_patch({"emptied.py": "E = 1\n"}, {"emptied.py": ""})
    assert "deleted file mode" not in patch and "+++ /dev/null" not in patch, patch
    assert _git_apply({"emptied.py": b"E = 1\n"}, patch, tmp_path / "applied") == {"emptied.py": b""}


def test_non_utf8_content_travels_as_a_git_binary_patch() -> None:
    base, files = CASES["non_utf8_bytes"]
    patch = unified_patch(base, files)
    patch.encode("utf-8")
    assert patch.count("GIT binary patch") == 3, patch


def test_identical_files_give_the_unchanged_marker() -> None:
    assert unified_patch({"app.py": "x = 1\n", "e.py": ""}, {"app.py": "x = 1\n", "e.py": ""}) == "--- a/empty\n+++ b/empty\n"
