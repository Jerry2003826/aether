"""Trusted check copied into a fresh workspace. It is not part of the snapshot."""

from __future__ import annotations

import importlib


def main() -> int:
    billing = importlib.import_module("billing")
    cases = ((1, 2, 3), (-4, 4, 0))
    failed = False
    for left, right, expected in cases:
        observed = billing.add(left, right)
        print(f"add({left}, {right})={observed}")
        if observed != expected:
            failed = True
    if failed:
        print("FAIL")
        return 1
    print("PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
