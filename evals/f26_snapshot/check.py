"""Independent check for the authorized snapshot. Not part of the agent tree."""

from billing import add


def main() -> int:
    observed = add(1, 2)
    other = add(-4, 4)
    print(f"add(1, 2)={observed}")
    print(f"add(-4, 4)={other}")
    if observed == 3 and other == 0:
        print("PASS")
        return 0
    print("FAIL")
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
