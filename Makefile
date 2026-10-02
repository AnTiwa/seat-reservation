.PHONY: up down test burst cancel-race multi-race logs metrics clean

up:
	docker compose up --build

down:
	docker compose down

test:
	pytest -v tests/test_concurrency.py

# One-command burst: hot-seat storm + idempotency + per-user-limit + multi-seat race
# Usage: make burst  OR  make burst URL=https://your-deployed-url.com
URL ?= http://localhost:8000
REQUESTS ?= 20000
burst:
	python tests/burst.py $(URL) $(REQUESTS)

cancel-race:
	python tests/cancel_race.py $(URL)

multi-race:
	python tests/multi_race.py $(URL)

logs:
	docker compose logs -f api

metrics:
	curl -s $(URL)/metrics/ | grep -E 'reservations_confirmed|reservations_declined|seats_available|http_request_duration|http_5xx'

clean:
	docker compose down -v --remove-orphans
