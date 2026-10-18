FROM python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/openhands/.venv

# git writes the agent's published patch (openhands_sandbox_entry._patch: git diff --no-index --binary).
# The image's identity is a hash of this file and its context (sandbox_tag), so the packages are fixed:
# apt reads only snapshot.debian.org at the base image's own snapshot (an immutable archive) and git is
# pinned to its exact version there. Taking Debian updates means moving the timestamp and the versions
# forward here, which gives a new tag.
RUN rm -f /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list \
 && printf '%s\n' \
    'Types: deb' \
    'URIs: https://snapshot.debian.org/archive/debian/20261005T000000Z' \
    'Suites: trixie trixie-updates' \
    'Components: main' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
    '' \
    'Types: deb' \
    'URIs: https://snapshot.debian.org/archive/debian-security/20261005T000000Z' \
    'Suites: trixie-security' \
    'Components: main' \
    'Signed-By: /usr/share/keyrings/debian-archive-keyring.pgp' \
    > /etc/apt/sources.list.d/aether-snapshot.sources \
 && apt-get -o Acquire::Check-Valid-Until=false update \
 && apt-get install -y --no-install-recommends git=1:2.47.3-0+deb13u1 git-man=1:2.47.3-0+deb13u1 \
 && rm -rf /var/lib/apt/lists/*

RUN pip install --no-cache-dir --only-binary :all: uv==0.13.1

WORKDIR /opt/openhands
COPY environments/openhands/pyproject.toml environments/openhands/uv.lock ./
COPY environments/openhands/wheels/ wheels/
RUN uv sync --locked --no-build --no-dev

COPY services/worker/aether_worker/coding_protocol.py /opt/aether/coding_protocol.py
COPY services/worker/aether_worker/grading_files.py /opt/aether/grading_files.py
COPY services/worker/aether_worker/openhands_sandbox_entry.py /opt/aether/openhands_sandbox_entry.py
RUN chmod -R a+rX /opt/openhands /opt/aether

LABEL aether.openhands_sandbox=1.54.0
USER nobody
