"""Process entry for the API. Bind address comes from the environment."""

from __future__ import annotations

import os

import uvicorn

from aether_api.app import create_app
from aether_api.settings import bind_is_allowed

app = create_app()


def main() -> None:
    host = os.environ.get("AETHER_BIND_HOST", "127.0.0.1")
    if not bind_is_allowed(host):
        raise SystemExit("refusing non-loopback bind without TLS certificate, TLS key, and secure cookies")
    kwargs: dict[str, str] = {}
    certificate = os.environ.get("AETHER_TLS_CERTFILE")
    key = os.environ.get("AETHER_TLS_KEYFILE")
    if certificate and key:
        kwargs["ssl_certfile"] = certificate
        kwargs["ssl_keyfile"] = key
    uvicorn.run(app, host=host, port=int(os.environ.get("AETHER_API_PORT", "8000")), **kwargs)


if __name__ == "__main__":
    main()
