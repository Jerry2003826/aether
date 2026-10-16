"""Pinned Playwright and Tor releases. Tags are not runtime references."""

from __future__ import annotations

PLAYWRIGHT_VERSION = "1.64.0"
CHROMIUM_REVISION = "1248"
CHROMIUM_BROWSER_VERSION = "156.0.8078.4"
# Manifest-list digest for mcr.microsoft.com/playwright:v1.64.0-noble, read 2026-10-16.
PLAYWRIGHT_IMAGE = "mcr.microsoft.com/playwright@sha256:06a9939e57531807f8d5fd76ce44b53165ffb7d7501d87ab10e285c20b1e971f"
# Manifest-list digest for debian:trixie-slim, read 2026-10-16.
DEBIAN_IMAGE = "debian@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f"
TOR_DISTRIBUTION = "trixie"
TOR_VERSION = "0.4.9.14-1~d13.trixie+1"
# deb.torproject.org archive signing key, read from the published asc on 2026-10-16.
TOR_KEY_FINGERPRINT = "A3C4F0F979CAA22CDBA8F512EE8CBC9E886DDD89"
TOR_ALIAS = "aether-tor"
TOR_SOCKS_PORT = 9050
APPROVED_TOR_PROXY = f"socks5://{TOR_ALIAS}:{TOR_SOCKS_PORT}"
PURPOSE = "threat_intelligence"
ENTRYPOINT = "aether_worker.threat_browser_entry"
PROBE = "aether_worker.threat_probe"
