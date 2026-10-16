#!/bin/sh
set -eu
trap '' HUP
config=/var/lib/tor/aether-torrc
cp /etc/tor/torrc "$config"
python3 - <<'PY'
import ipaddress
import os
import sys
from pathlib import Path

address = os.environ.get("AETHER_TOR_SOCKS_ADDRESS", "")
policy = os.environ.get("AETHER_TOR_SOCKS_POLICY", "")
try:
    network = ipaddress.ip_network(policy, strict=True)
    host = ipaddress.ip_address(address)
except ValueError:
    sys.exit(1)
supernet = ipaddress.ip_network("10.240.0.0/16")
if network.prefixlen != 28 or not network.subnet_of(supernet):
    sys.exit(1)
if host not in network or host in {network.network_address, network.broadcast_address}:
    sys.exit(1)
path = Path("/var/lib/tor/aether-torrc")
path.write_text(
    path.read_text(encoding="utf-8")
    + f"\nSocksPort {host}:9050\nSocksPolicy accept {network}\nSocksPolicy reject *\n",
    encoding="utf-8",
)
PY
if [ "${AETHER_TOR_DISABLE_NETWORK:-}" = "1" ]; then
  printf '\nDisableNetwork 1\n' >> "$config"
fi
if [ "${AETHER_TOR_FIXTURE:-}" = "1" ]; then
  openssl req -x509 -newkey rsa:2048 -sha256 -days 2 -nodes \
    -keyout /var/lib/tor/fixture/key.pem \
    -out /var/lib/tor/fixture/cert.pem \
    -subj "/CN=aether-tor" \
    -addext "subjectAltName=DNS:aether-tor" \
    -addext "basicConstraints=critical,CA:TRUE"
  chmod 600 /var/lib/tor/fixture/key.pem
  chmod 644 /var/lib/tor/fixture/cert.pem
  python3 /usr/local/lib/aether-tor/fixture_server.py >/var/lib/tor/fixture/server.log 2>&1 &
fi
exec tor -f "$config"
