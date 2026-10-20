"""The sandbox tag hash sees every COPY/ADD source, whatever the instruction layout."""

from __future__ import annotations

import pytest

from aether_worker.openhands_image import _copy_sources


def test_continued_and_json_array_copy_sources_are_found() -> None:
    dockerfile = (
        "FROM python@sha256:abc\n"
        "RUN echo a \\\n    && echo b\n"
        "COPY environments/openhands/pyproject.toml \\\n"
        "     environments/openhands/uv.lock \\\n"
        "     ./\n"
        'COPY ["services/worker/aether_worker/coding_protocol.py", "/opt/aether/coding_protocol.py"]\n'
        'ADD --chown=nobody ["a b/c.py", "d.py", "/opt/x/"]\n'
        "copy --chmod=0644 lower.py /opt/lower.py\n"
        "# COPY ignored.py /nowhere\n"
        "COPY --from=builder /built /opt/built\n"
    )
    assert _copy_sources(dockerfile) == [
        "environments/openhands/pyproject.toml",
        "environments/openhands/uv.lock",
        "services/worker/aether_worker/coding_protocol.py",
        "a b/c.py",
        "d.py",
        "lower.py",
    ]


@pytest.mark.parametrize(
    "line",
    [
        "COPY <<EOF /opt/x\nhello\nEOF\n",
        'COPY ["only-one"]\n',
        "COPY onlyone\n",
        'COPY ["broken", \n',
        "ADD https://example.com/x.tar.gz /opt/\n",
    ],
)
def test_unrecognised_copy_forms_fail_loudly(line: str) -> None:
    with pytest.raises(ValueError):
        _copy_sources("FROM scratch\n" + line)


def test_the_real_dockerfile_still_parses() -> None:
    from pathlib import Path

    from aether_worker.openhands_image import DOCKERFILE, repository_root

    sources = _copy_sources((repository_root() / DOCKERFILE).read_text(encoding="utf-8"))
    assert "environments/openhands/uv.lock" in sources
    assert "services/worker/aether_worker/grading_files.py" in sources
    assert all(not Path(item).is_absolute() for item in sources)
