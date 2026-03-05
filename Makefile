.PHONY: demo demo-down demo-logs bitcoind participant test test-e2e

demo:
	docker compose up --build

# Multi-PC: levanta solo bitcoind en PC-A (los participantes corren en sus propias máquinas)
bitcoind:
	docker compose up bitcoind

# Multi-PC: arranca el servidor participante leyendo .env.local
participant:
	bash scripts/run_participant.sh

demo-down:
	docker compose down -v

demo-logs:
	docker compose logs -f

test:
	python -m pytest tests/test_protocol.py tests/test_coordinator.py -v

test-e2e:
	bash scripts/regtest_setup.sh && python -m pytest tests/test_e2e_regtest.py -v -s
