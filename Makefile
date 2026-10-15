.PHONY: check-pack test-reference doctor test-unit test-integration test-security acceptance-report demo-live backup restore

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

test-security:
	AETHER_REQUIRE_SERVICES=1 $(UV) run pytest -q tests/unit/test_attack_carriers.py tests/integration/test_security_regression.py

acceptance-report:
	$(UV) run python -c 'from aether_worker.acceptance_report import build_report; print(build_report(__import__("pathlib").Path("reports/junit.xml"), __import__("pathlib").Path("STATUS.json")))'

demo-live:
	$(UV) run python -c 'from aether_worker.live_harness import run_live_probe; import json,sys; report=run_live_probe(); print(json.dumps(report, ensure_ascii=False)); sys.exit({"probed": 0, "failed": 1, "blocked": 2}[report["status"]])'

backup:
	@echo 'backup 需要 AETHER_ADMIN_DATABASE_URL、产物目录和目标目录，见 services/worker/aether_worker/supply.py'

restore:
	@echo 'restore 需要确认词 restore-aether，不会执行 docker compose down -v'
