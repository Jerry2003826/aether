FROM debian@sha256:a29215f6a35e51e22adffa17f89e9d2ef06214e64a2bad10d765c46aea49f11f
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends ca-certificates gnupg python3 openssl \
 && rm -rf /var/lib/apt/lists/* \
 && printf '%s\n' '#!/bin/sh' 'exit 101' > /usr/sbin/policy-rc.d \
 && chmod 755 /usr/sbin/policy-rc.d
COPY infra/tor/install_tor.py /usr/local/lib/aether-tor/install_tor.py
RUN python3 /usr/local/lib/aether-tor/install_tor.py && rm /usr/local/lib/aether-tor/install_tor.py
COPY infra/tor/torrc /etc/tor/torrc
COPY infra/tor/fixture_server.py /usr/local/lib/aether-tor/fixture_server.py
COPY infra/tor/entrypoint.sh /usr/local/bin/aether-tor-entry
RUN mkdir -p /var/lib/tor/fixture \
 && chmod 755 /usr/local/bin/aether-tor-entry /usr/local/lib/aether-tor/fixture_server.py \
 && chown -R debian-tor:debian-tor /var/lib/tor \
 && chmod 700 /var/lib/tor /var/lib/tor/fixture
USER debian-tor
ENTRYPOINT ["/usr/local/bin/aether-tor-entry"]
