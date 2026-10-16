#!/bin/sh
set -eu
trap '' HUP
config=/var/lib/tor/aether-torrc
cp /etc/tor/torrc "$config"
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
