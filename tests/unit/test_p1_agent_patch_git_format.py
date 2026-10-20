"""The agent's patch is published when it is in git's own format, and still not when it carries no change.

The patches here are what git itself writes (via unified_patch, which runs git diff --no-index
--binary): a modification, a binary file, a new file, a deleted file and empty files created or
deleted. Each must be published as the agent's change and reach the terminal state as such. A git
header without any change below it is still rejected, like the existing "diff --git a/app.py
b/app.py" case in test_coding_skill_pipeline.py.
"""

from __future__ import annotations

import pytest
from aether_worker.coding_jobs import (
    _agent_patch,
    _coding_terminal,
    _patch_source,
    _published_patch_or_error,
)
from aether_worker.coding_workspace import unified_patch

_BASE = {"app.py": "def value():\n    return 0\n"}

GIT_PATCHES = {
    "modified": (_BASE, {"app.py": "def value():\n    return 42\n"}),
    "binary": ({**_BASE, "blob.py": b"\x00\x01\x02"}, {**_BASE, "blob.py": b"\x00\x01\x03\xff"}),
    "new_file": (_BASE, {**_BASE, "pkg/mod.py": "NEW = 1\n"}),
    "deleted_file": ({**_BASE, "gone.py": "G = 1\n"}, _BASE),
    "empty_created": (_BASE, {**_BASE, "pkg/__init__.py": ""}),
    "empty_deleted": ({**_BASE, "legacy.py": ""}, _BASE),
    "all_mixed": (
        {**_BASE, "blob.py": b"\x00\x01", "gone.py": "G = 1\n", "legacy.py": ""},
        {"app.py": "def value():\n    return 42\n", "blob.py": b"\x00\x02", "pkg/__init__.py": "", "new.py": "N = 1\n"},
    ),
}


def _sandbox(patch: object) -> dict:
    return {
        "exit_code": 0,
        "result": {
            "passed": 1,
            "failed": 0,
            "agent_completed": True,
            "model_requests": 1,
            "edit_calls": 1,
            "patch": patch,
        },
    }


@pytest.mark.parametrize("case", sorted(GIT_PATCHES))
def test_a_git_format_agent_patch_is_published(case: str) -> None:
    base, files = GIT_PATCHES[case]
    patch = unified_patch(base, files)
    assert patch.startswith("diff --git "), patch
    sandbox = _sandbox(patch)
    assert _agent_patch(sandbox) == patch
    assert _patch_source(sandbox) == "agent_edit"
    workspace = {"base_files": {"app.py": "def value():\n    return 0\n"}, "files": {"app.py": "x = 1\n"}, "tests": {}}
    assert _published_patch_or_error(workspace, sandbox, job_id="job") == (patch.encode(), None)
    assert _coding_terminal(sandbox, upstream_error=None, measured=(8, 4), usage_missing=False, channel_revoked=True) == (
        "FINISHED",
        None,
    )


@pytest.mark.parametrize(
    "patch",
    [
        "diff --git a/app.py b/app.py\n",
        "diff --git a/app.py b/app.py\nindex 351b79b..7958e41 100644\n",
        "diff --git a/app.py b/app.py\ndiff --git a/x.py b/x.py\n",
        "diff --git a/blob.py b/blob.py\nindex 1..2 100644\nBinary files a/blob.py and b/blob.py differ\n",
        "diff --git",
        "diff --gitx a/app.py b/app.py\n--- a/app.py\n+++ b/app.py\n",
    ],
    ids=["header_only", "index_only", "two_headers", "binary_without_data", "truncated", "not_a_git_header"],
)
def test_a_git_header_with_no_change_is_still_not_the_agents_patch(patch: str) -> None:
    sandbox = _sandbox(patch)
    assert _agent_patch(sandbox) is None
    assert _patch_source(sandbox) == "candidate_files"
    assert _coding_terminal(sandbox, upstream_error=None, measured=(8, 4), usage_missing=False, channel_revoked=True) == (
        "FAILED",
        "agent_no_change",
    )
