FROM python@sha256:70729b46c69b4f1e97c4822c1af3df53a1476cf5ddc6c087c0c10bc3a5678c2f

ENV PYTHONDONTWRITEBYTECODE=1 \
    UV_PYTHON=/usr/local/bin/python \
    UV_PYTHON_DOWNLOADS=never \
    UV_LINK_MODE=copy \
    UV_PROJECT_ENVIRONMENT=/opt/openhands/.venv

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
