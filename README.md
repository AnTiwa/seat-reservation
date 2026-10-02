# Seat Reservation Service

A concurrency-safe, production-grade seat reservation API built with **FastAPI** and **PostgreSQL**.

The service handles atomic multi-seat reservations, idempotent retries, per-user seat limits, explicit cancellation, health checks, Prometheus metrics, and structured JSON logging — and is verified correct under thousands of concurrent requests.

---

## Table of Contents

1. [Tech Stack](#tech-stack)
2. [Quick Start (Docker)](#quick-start-docker)
3. [One-Command Burst Test](#one-command-burst-test)
4. [API Reference](#api-reference)
5. [Health Checks](#health-checks)
6. [Metrics](#metrics)
7. [Logs](#logs)
8. [Concurrency Model](#concurrency-model)
9. [Idempotency](#idempotency)
10. [Per-User Seat Limit](#per-user-seat-limit)
11. [Cancellation](#cancellation)
12. [Correctness Guarantees](#correctness-guarantees)
13. [Project Structure](#project-structure)
14. [Running Tests](#running-tests)
15. [Live URL](#live-url)

---

## Tech Stack

| Component | Technology |
|-----------|------------|
| Runtime | Python 3.12 |
| Framework | FastAPI |
| Database | PostgreSQL 17 |
| DB driver | psycopg 3 + connection pool |
| Metrics | prometheus-client |
| Container | Docker / Docker Compose |
| Server | Uvicorn |

---

## Quick Start (Docker)

```bash
# Clone and start everything (API + PostgreSQL)
docker compose up --build
```

The API is available at `http://localhost:8000`.

| URL | Description |
|-----|-------------|
| `http://localhost:8000/docs` | Interactive Swagger UI |
| `http://localhost:8000/health/live` | Liveness check |
| `http://localhost:8000/health/ready` | Readiness check (DB-backed) |
| `http://localhost:8000/metrics/` | Prometheus metrics |

Clean rebuild from scratch:

```bash
docker compose down -v --remove-orphans
docker compose up --build
```

---

## One-Command Burst Test

Reproduce the on-sale stampede against any URL:

```bash
# Against localhost (default)
make burst

# Or directly
python tests/burst.py http://localhost:8000 20000
```

The burst script runs four scenarios in sequence:

| Scenario | Description |
|----------|-------------|
| **Hot-seat storm** | 20,000 users fight for the same seat — exactly 1 wins |
| **Idempotency concurrency** | 100 concurrent retries with the same key — all return the same reservation |
| **Per-user limit** | 20 concurrent requests from one user on a limit=4 show — exactly 4 confirmed |
| **Multi-seat race** | Two users request overlapping seats simultaneously — one wins, one loses |

Expected output:

```
=== HOT SEAT STORM ===
outcomes: Counter({'seat-taken': 19999, 'confirmed': 1})
HOT SEAT: PASS

=== IDEMPOTENCY CONCURRENCY ===
outcomes: Counter({'confirmed': 100})
unique reservation IDs: 1
IDEMPOTENCY: PASS

=== PER-USER LIMIT ===
outcomes: Counter({'per-user-limit': 16, 'confirmed': 4})
PER-USER LIMIT: PASS

=== MULTI-SEAT RACE ===
MULTI-SEAT: PASS

========================================
ALL BURST TESTS: PASS
========================================
```

Additional race tests:

```bash
make cancel-race    # 100 concurrent cancels of the same reservation
make multi-race     # overlapping multi-seat race (standalone)
```

---

## API Reference

### POST /shows — Create a show (admin)

```bash
curl -X POST http://localhost:8000/shows \
  -H 'Content-Type: application/json' \
  -d '{
    "name": "friday-night",
    "seats": ["A1", "A2", "A3", "A4"],
    "price_paise": 25000,
    "per_user_limit": 4
  }'
```

Response `201`:

```json
{
  "id": "<SHOW_ID>",
  "name": "friday-night",
  "seats": [
    {"seat_number": "A1", "status": "available"},
    {"seat_number": "A2", "status": "available"},
    {"seat_number": "A3", "status": "available"},
    {"seat_number": "A4", "status": "available"}
  ],
  "price_paise": 25000,
  "per_user_limit": 4
}
```

---

### POST /shows/{id}/reserve — Reserve seats

Identity is taken from the `Authorization: Bearer <user_id>` header — never from the request body.

Required header: `Idempotency-Key: <uuid>` (must be unique per logical operation).

```bash
curl -X POST http://localhost:8000/shows/<SHOW_ID>/reserve \
  -H 'Authorization: Bearer alice' \
  -H 'Idempotency-Key: req-001' \
  -H 'Content-Type: application/json' \
  -d '{"seats": ["A1"]}'
```

Success `201`:

```json
{
  "reservation_id": "<UUID>",
  "show_id": "<SHOW_ID>",
  "user_id": "alice",
  "seats": ["A1"],
  "amount_paise": 25000,
  "status": "confirmed"
}
```

Error responses:

| HTTP | Condition |
|------|-----------|
| `400` | Missing `Idempotency-Key` header, or duplicate seats in request body |
| `401` | Missing or invalid `Authorization` header |
| `404` | Show or seat not found |
| `409` | Seat already taken, per-user limit exceeded, or idempotency key reused with different body |

---

### GET /shows/{id} — Show state

```bash
curl http://localhost:8000/shows/<SHOW_ID>
```

Response:

```json
{
  "id": "<SHOW_ID>",
  "name": "friday-night",
  "price_paise": 25000,
  "per_user_limit": 4,
  "total_seats": 4,
  "counts": {
    "available": 3,
    "held": 0,
    "confirmed": 1
  },
  "seats": [
    {"seat_number": "A1", "status": "confirmed"},
    {"seat_number": "A2", "status": "available"},
    {"seat_number": "A3", "status": "available"},
    {"seat_number": "A4", "status": "available"}
  ]
}
```

The reconciliation invariant `available + held + confirmed == total_seats` is asserted server-side on every response.

---

### POST /reservations/{id}/cancel — Cancel a reservation

Only the reservation owner can cancel.

```bash
curl -X POST http://localhost:8000/reservations/<RESERVATION_ID>/cancel \
  -H 'Authorization: Bearer alice'
```

Response `200`:

```json
{"reservation_id": "<UUID>", "status": "cancelled"}
```

Cancellation is idempotent — repeated calls return `200 cancelled` without double-releasing seats.

---

## Health Checks

### Liveness — `GET /health/live`

Returns `200 {"status": "ok"}` if the process is running. Always `200` as long as the process is alive.

### Readiness — `GET /health/ready`

Executes `SELECT 1` against PostgreSQL. Returns:
- `200 {"status": "ready"}` — database reachable.
- `503 {"status": "not_ready"}` — database unreachable → load balancer stops routing traffic.

---

## Metrics

Prometheus metrics are exposed at `GET /metrics/`.

```bash
curl -s http://localhost:8000/metrics/ \
  | grep -E 'reservations_confirmed|reservations_declined|seats_available|http_request_duration|http_5xx'
```

| Metric | Type | Description |
|--------|------|-------------|
| `reservations_confirmed_total` | Counter | Total successful reservations |
| `reservations_declined_total{reason}` | Counter | Declines by reason label |
| `seats_available{show_id}` | Gauge | Available seats per show |
| `http_request_duration_seconds` | Histogram | Request latency by method/path/status class |
| `http_5xx_total` | Counter | Unhandled server errors |

Decline reason labels:

- `seat-taken`
- `per-user-limit`
- `idempotent-replay`
- `seat-not-found`
- `invalid-request`

---

## Logs

Structured JSON logs are written to **stdout** and to a **timestamped file** inside `/app/logs/` (or the path set by `LOG_DIR`).

Each Docker startup creates a new file, e.g. `app-20241015T093012Z.log`.

```bash
# Live tail via Docker
make logs
# or
docker compose logs -f api

# Access the log volume
docker run --rm -v seat-reservation-main_api_logs:/logs alpine ls -lh /logs
```

Every request log line:

```json
{
  "event": "request",
  "request_id": "550e8400-e29b-41d4-a716-446655440000",
  "method": "POST",
  "path": "/shows/abc/reserve",
  "status": 201,
  "duration_ms": 14.2
}
```

Pass `X-Request-ID: <id>` on requests to set a custom correlation ID.

---

## Concurrency Model

PostgreSQL is the system of record for all concurrency control. No in-memory locks are used, so multiple API replicas can safely serve the same database.

The reservation transaction acquires locks in this fixed order to prevent deadlocks:

```
1. idempotency_keys row   (INSERT ON CONFLICT / SELECT FOR UPDATE)
2. user_show_counters row (SELECT FOR UPDATE)  — serialises per-user limit check
3. seats rows             (SELECT FOR UPDATE ORDER BY seat_number)  — sorted to prevent A+B vs B+A deadlock
```

The key correctness property: locks are acquired **before** the availability check. A concurrent transaction cannot slip through the check while another holds the lock.

---

## Idempotency

Every reservation request requires an `Idempotency-Key` header. The key is stored in the `idempotency_keys` table with a `PRIMARY KEY (show_id, idempotency_key)` constraint.

| Scenario | Outcome |
|----------|---------|
| First request | Reservation created, `reservation_id` stored |
| Retry (same key, same seats) | Original reservation returned, HTTP 201 |
| Retry (same key, different seats) | HTTP 409 — `idempotency key already used with different request` |
| Concurrent retries (same key) | All wait on `SELECT FOR UPDATE`; all return the same reservation after the first commits |

---

## Per-User Seat Limit

Each show has a `per_user_limit` (default 4). A `user_show_counters` row tracks each `(show_id, user_id)` pair and is locked with `SELECT FOR UPDATE` before the limit check, serialising all parallel requests from the same user.

Exceeding the limit returns:
```json
HTTP 409
{"detail": "per-user seat limit exceeded"}
```

---

## Cancellation

`POST /reservations/{id}/cancel` is fully transactional. The locking order mirrors the reservation path to avoid deadlocks:

1. Lock `user_show_counters` row
2. Lock `reservations` row
3. Lock `reservation_seats` rows (sorted)
4. Set seats to `available`
5. Set reservation to `cancelled`
6. Decrement user counter
7. Commit

An already-cancelled reservation returns `200 cancelled` immediately (idempotent, no second release).

---

## Correctness Guarantees

All of the following must hold — and are verified by the burst and pytest suites:

| Invariant | Mechanism |
|-----------|-----------|
| No double-sell | `SELECT FOR UPDATE` before availability check |
| All-or-nothing multi-seat | Single transaction; any unavailable seat rolls back the whole request |
| Per-user limit under concurrency | `SELECT FOR UPDATE` on counter row; checked before seat lock |
| Idempotent retries | `PRIMARY KEY (show_id, idempotency_key)` + `SELECT FOR UPDATE` |
| Same-key, different-body → 409 | SHA-256 hash comparison after key lock |
| Reconciliation invariant | Asserted in `GET /shows`; no held state created without confirmed state |
| Owner-only cancellation | `user_id` from token compared to reservation's `user_id` |
| Cancel idempotency | Already-cancelled status check before lock acquisition |

---

## Project Structure

```text
.
├── app/
│   ├── __init__.py
│   ├── auth.py             # Bearer token → user_id
│   ├── db.py               # psycopg connection pool
│   ├── logging_config.py   # Structured JSON logging + per-startup file
│   ├── main.py             # FastAPI routes
│   ├── metrics.py          # Prometheus counters/gauges/histograms
│   └── schema.sql          # PostgreSQL DDL (idempotent)
├── tests/
│   ├── conftest.py         # Shared pytest fixtures
│   ├── burst.py            # One-command stampede script
│   ├── cancel_race.py      # Concurrent cancellation test
│   ├── multi_race.py       # Overlapping multi-seat race
│   └── test_concurrency.py # pytest integration test suite
├── Dockerfile
├── docker-compose.yml
├── Makefile
├── pytest.ini
├── requirements.txt
├── README.md
└── WRITEUP.md
```

---

## Running Tests

Start the stack first:

```bash
docker compose up --build -d
```

Then:

```bash
# Full pytest integration suite
make test

# Hot-seat + idempotency + per-user-limit + multi-seat burst
make burst

# Concurrent cancellation race
make cancel-race

# Overlapping multi-seat race (standalone)
make multi-race
```

---

## Live URL

> **URL:** https://seat-reservation-zzba.onrender.com/

### How to Authorize in Swagger

1. Open the live URL above.
2. On the Swagger page, look at the **top-right corner** for the **Authorize** button with the 🔒 padlock icon.
3. Click **Authorize**.
4. In the authorization dialog, enter:
   `alice`
5. Click **Save** (or **Authorize**, depending on the Swagger UI version).
6. Authorization is now applied to the API requests that require authentication.
8. You can now open an individual endpoint and click **Try it out** to test it.

> **Note:** You do **not** need to manually add the `Authorization` header to each individual route. Authorize once using the **Authorize** button at the top of the Swagger page.
