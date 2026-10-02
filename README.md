# Seat Reservation Service

A concurrency-safe, production-grade seat reservation API built with **FastAPI** and **PostgreSQL**.

The service handles atomic multi-seat reservations, idempotent retries, per-user seat limits, explicit cancellation, health checks, Prometheus metrics, and structured JSON logging — and is verified correct under thousands of concurrent requests.

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
│   └── burst.py            # One-command stampede script
├── Dockerfile
├── docker-compose.yml
├── requirements.txt
├── README.md
```
