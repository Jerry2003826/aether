"""Process entry for the API. Bind address comes from the environment."""

from __future__ import annotations

import os

import uvicorn

from aether_api.health import create_app

app = create_app()


def main() -> None:
    host = os.environ.get("AETHER_BIND_HOST", "127.0.0.1")
    if host not in {"127.0.0.1", "::1"} and os.environ.get("AETHER_ALLOW_NON_LOOPBACK") != "1":
        raise SystemExit("refusing non-loopback bind without AETHER_ALLOW_NON_LOOPBACK=1")
    uvicorn.run(app, host=host, port=int(os.environ.get("AETHER_API_PORT", "8000")))


if __name__ == "__main__":
    main()
