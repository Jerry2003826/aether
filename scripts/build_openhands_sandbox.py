"""Build the digest-based OpenHands coding image. The control-plane JOB_IMAGE constant is not changed."""

from aether_worker.openhands_image import build_openhands_sandbox, coding_runtime_image


def main() -> None:
    image_id = build_openhands_sandbox()
    found = coding_runtime_image()
    if found is None:
        raise SystemExit("openhands sandbox image has no aether.openhands_sandbox label")
    print(image_id)


if __name__ == "__main__":
    main()
