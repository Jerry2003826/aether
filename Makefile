.PHONY: check-pack test-reference doctor test-unit test-integration

UV ?= uv

check-pack:
	$(UV) run python scripts/verify_freeze.py

test-reference:
	$(UV) run python -m unittest discover -s tests -p 'test_*.py' -v

doctor:
	$(UV) run python scripts/doctor.py --require-services

test-unit:
	$(UV) run pytest -q tests/unit

test-integration:
	AETHER_REQUIRE_SERVICES=1 $(UV) run pytest -q tests/integration
