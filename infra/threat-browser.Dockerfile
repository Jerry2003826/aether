FROM mcr.microsoft.com/playwright@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f
USER root
ENV PLAYWRIGHT_BROWSERS_PATH=/ms-playwright
ENV PIP_DISABLE_PIP_VERSION_CHECK=1
ENV PYTHONPATH=/opt/aether
ENV PYTHONUNBUFFERED=1
ENV HOME=/tmp
RUN apt-get update \
 && apt-get install -y --no-install-recommends python3 python3-pip \
 && pip3 install --no-cache-dir --break-system-packages --only-binary=:all: playwright==1.64.0 pyyaml==6.0.3 \
 && rm -rf /var/lib/apt/lists/*
COPY packages/domain/aether_domain /opt/aether/aether_domain
COPY services/worker/aether_worker/__init__.py /opt/aether/aether_worker/__init__.py
COPY services/worker/aether_worker/net_guard.py /opt/aether/aether_worker/net_guard.py
COPY services/worker/aether_worker/threat_pins.py /opt/aether/aether_worker/threat_pins.py
COPY services/worker/aether_worker/threat_browser.py /opt/aether/aether_worker/threat_browser.py
COPY services/worker/aether_worker/threat_browser_entry.py /opt/aether/aether_worker/threat_browser_entry.py
COPY services/worker/aether_worker/threat_probe.py /opt/aether/aether_worker/threat_probe.py
COPY config/threat_browser.yaml /opt/aether/config/threat_browser.yaml
USER pwuser
