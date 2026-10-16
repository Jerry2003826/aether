"""Pinned sandbox base. A mutable tag is not a release reference."""

from __future__ import annotations

SANDBOX_IMAGE = "python@sha256:1b668429b3511ab407d8e00648891631b0b1a4d7e15e3ca70f38ab5b91ad4ab4"

# Wheels resolved inside that image (CPython 3.12, pip 25.0.1).
_PYTEST_LINES = (
    "pytest==8.3.5 \\",
    "    --hash=sha256:c69214aa47deac29fad6c2a4f590b9c4a9fdb16a403176fe154b79c0b4d4d820",
    "pluggy==1.6.0 \\",
    "    --hash=sha256:e920276dd6813095e9377c0bc5566d94c932c33b27a3e3945d8389c374dd4746",
    "iniconfig==2.1.0 \\",
    "    --hash=sha256:9deba5723312380e77435581c6bf4935c94cbfab9b1ed33ef8d238ea168eb760",
    "packaging==25.0 \\",
    "    --hash=sha256:29572ef2b1f17581046b3a2227d5c611fb25ec70ca1ba8554b24b0e69331a484",
)


def image_is_digest_pinned(reference: str) -> bool:
    _name, separator, digest = reference.partition("@")
    if separator != "@" or not digest.startswith("sha256:"):
        return False
    hexpart = digest.removeprefix("sha256:")
    return len(hexpart) == 64 and all(character in "0123456789abcdef" for character in hexpart)


def grader_dockerfile() -> str:
    quoted = " ".join(repr(line) for line in _PYTEST_LINES)
    return (
        f"FROM {SANDBOX_IMAGE}\n"
        f"RUN printf '%s\\n' {quoted} > /tmp/requirements.txt \\\n"
        " && pip install --no-cache-dir --require-hashes --only-binary=:all: -r /tmp/requirements.txt\n"
    )
