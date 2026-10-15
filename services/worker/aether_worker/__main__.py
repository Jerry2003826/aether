"""Process entry: python -m aether_worker"""

from __future__ import annotations

import asyncio

from aether_worker.service import serve


def main() -> None:
    asyncio.run(serve())


if __name__ == "__main__":
    main()
