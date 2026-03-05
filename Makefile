.PHONY: demo demo-down demo-logs test test-e2e

demo:
	docker compose up --build

demo-down:
	docker compose down -v

demo-logs:
	docker compose logs -f

test:
	python -m pytest tests/test_protocol.py tests/test_coordinator.py -v

test-e2e:
	bash scripts/regtest_setup.sh && python -m pytest tests/test_e2e_regtest.py -v -s
