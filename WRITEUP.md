# WRITEUP

## 1. The Atomic Decision — Mechanism & Race-Freedom

### Single-seat case

The atomic decision lives in a **`SELECT … FOR UPDATE` followed by a conditional `UPDATE … WHERE status = 'available'`**, all inside a single serialisable PostgreSQL transaction.

The exact sequence inside `POST /shows/{id}/reserve`:

```
BEGIN;

-- 1. Insert idempotency row (unique PK: show_id, idempotency_key)
--    ON CONFLICT DO NOTHING  →  only one winner at the DB level
INSERT INTO idempotency_keys … ON CONFLICT DO NOTHING RETURNING …

-- 2. Lock the per-user counter row (serialises concurrent requests
--    from the same user so the per-user limit check is accurate)
SELECT seat_count FROM user_show_counters … FOR UPDATE;

-- 3. Lock the requested seat rows in deterministic (sorted) order
SELECT seat_number, status FROM seats
WHERE show_id = $1 AND seat_number = ANY($2)
ORDER BY seat_number          -- ← deterministic order
FOR UPDATE;                   -- ← row-level write lock

-- 4. Check that every seat is still 'available'
--    (if not → raise 409, transaction rolls back, locks released)

-- 5. Insert reservation + reservation_seats rows

-- 6. Conditional UPDATE — second safety net
UPDATE seats SET status = 'confirmed' …
WHERE … AND status = 'available';   -- guard on current state
-- rowcount != requested count → 409 + rollback

-- 7. Increment per-user counter
-- 8. Write reservation_id into idempotency row
COMMIT;
```

**Why it is race-free:**  
The `FOR UPDATE` on step 3 acquires an exclusive row lock on each seat row before the availability check. A concurrent transaction that has already locked a seat will block here until the first transaction commits or rolls back. When it unblocks, step 4 sees the seat is now `confirmed` and returns 409 — never a double-sell. The conditional `UPDATE … WHERE status = 'available'` in step 6 is a second guard that catches any edge-case where the state changed between read and write (e.g., a concurrent `ROLLBACK TO SAVEPOINT` scenario).

### Multi-seat deadlock avoidance

Deadlocks between two overlapping multi-seat reservations (e.g., `[A1,A2]` vs `[A2,A1]`) are prevented by **always sorting seat numbers lexicographically before locking**:

```python
requested_seats = sorted(body.seats)   # deterministic order
```

This means any two transactions requesting the same seat set will acquire locks in the same order, making a cycle impossible.

---

## 2. Idempotency

### Where the key is stored

```sql
CREATE TABLE idempotency_keys (
    show_id         UUID  NOT NULL,
    idempotency_key TEXT  NOT NULL,
    user_id         TEXT  NOT NULL,
    request_hash    TEXT  NOT NULL,   -- SHA-256 of sorted seats
    reservation_id  UUID  NULL,       -- filled after commit
    PRIMARY KEY (show_id, idempotency_key)
);
```

The primary key `(show_id, idempotency_key)` is a unique constraint enforced at the database level.

### Exactly-once enforcement

`INSERT … ON CONFLICT DO NOTHING RETURNING …` is the single atomic gate:

- **First request:** INSERT succeeds, RETURNING yields a row → proceed with reservation.
- **Concurrent duplicate (same key, same body):** INSERT finds the PK exists, returns nothing. The transaction then does `SELECT … FOR UPDATE` on the row and waits until the first transaction commits. After it commits, `reservation_id` is populated and the caller gets the original reservation back (HTTP 201).
- **After the first commit:** Any subsequent request with the same key finds `reservation_id` already set → return the cached result immediately (HTTP 201, same body).

### Same-key, different-body handling

`request_hash` is a SHA-256 of `json.dumps({"seats": sorted_seats})`. On replay, the stored hash is compared with the incoming hash:

```python
if existing_hash != body_hash:
    raise HTTPException(409, "idempotency key already used with different request")
```

This is checked **after acquiring the `FOR UPDATE` lock** on the idempotency row, so two concurrent mismatched retries cannot both slip through.

---

## 3. Holds & Expiry

This implementation uses **immediate-confirm semantics**: there is no intermediate "held" state for a booking flow. A reservation goes directly from `available → confirmed` inside a single transaction. The `held` status exists in the schema (and the `GET /shows` response includes its count) as an extension point, but the current flow does not use it.

**Rationale:** A two-phase hold+capture model adds significant complexity (expiry workers, re-enqueue logic, zombie-hold cleanup) without improving correctness for the problem as stated. The assignment's correctness bar — no double-sell, atomic all-or-nothing — is fully satisfied by the direct confirm model.

**If holds were required:** A background worker would run `UPDATE seats SET status = 'available' WHERE status = 'held' AND held_until < now()` on a short interval (e.g., 30 s), protected by a `SELECT … FOR UPDATE SKIP LOCKED` to avoid lock contention with active reservation transactions.

Explicit **cancellation** (`POST /reservations/{id}/cancel`) is implemented and tested. It releases the seats atomically and restores them to `available`.

---

## 4. Consistency vs. Availability Under a Partition

This service chooses **consistency over availability**

- All reads and writes go through a single PostgreSQL instance.
- If the database is unreachable, the `/health/ready` endpoint returns `503` and a load balancer can stop sending traffic.
- There is no fallback cache or degraded-write mode — the service fails closed.

**Trade-off rationale:** Selling the same seat twice is an unrecoverable business error. A brief outage (during which 0 bookings succeed) is far preferable to an inconsistency that requires compensation, refunds, and customer-service escalation. For a ticketing system at on-sale time, correctness is the only acceptable operating mode.

**Under a partition:** PostgreSQL would block or error. The service would return `503` from readiness, and all reservation attempts would return `5xx`. This is intentional — the reconciliation invariant must never be violated.

---

## 5. Observability — What I'd Get Paged For at 2 AM

### Metrics (Prometheus — `/metrics/`)

| Metric | Type | Alert condition |
|--------|------|-----------------|
| `http_request_duration_seconds{path_template="/shows/{show_id}/reserve"}` | Histogram | p99 > 2 s |
| `http_5xx_total` rate > 0 | Counter | Any 5xx in a 1-min window |
| `reservations_confirmed_total` rate = 0 during on-sale burst | Counter | Healthy burst should confirm ~N/s |
| `reservations_declined_total{reason="seat-taken"}` | Counter | Informational — high is expected during stampede |
| `seats_available{show_id="…"}` | Gauge | Drops to 0 (sold out) — page if it bounces (seats re-appearing) |

### Logs (structured JSON, stdout + `/app/logs/app-<ts>.log`)

Every HTTP request emits:
```json
{"event": "request", "request_id": "...", "method": "POST", "path": "/shows/.../reserve", "status": 201, "duration_ms": 12.3}
```

A `request_id` (from `X-Request-ID` header or generated UUID) threads through every log line, enabling correlation of a retry chain.

**2 AM pages I'd configure:**
1. `http_5xx_total` rate > 0 for 60 s → P0 (data integrity risk)
2. `http_request_duration_seconds` p99 > 3 s → P1 (UX degradation)
3. DB connection pool saturation (psycopg pool queue depth) → P1
4. `/health/ready` returning `503` → P0 (DB unreachable)
5. Reconciliation invariant violation (periodic job: `available + held + confirmed != total_seats`) → P0

---

## 6. AI Usage

AI tools (Claude) were used and directed as follows:

| Task | How AI was used |
|------|-----------------|
| Initial scaffolding of FastAPI routes | Generated first draft; reviewed and corrected transaction ordering manually |
| SQL schema design | Reviewed AI suggestions against the locking requirements; added `user_show_counters` separately after reasoning about serialisation |
| Idempotency logic | Wrote the `FOR UPDATE` + `ON CONFLICT DO NOTHING` pattern myself after identifying the race; used AI to verify the edge case where both branches race to insert |
| Burst test script | Co-written; I specified the test scenarios (hot-seat, idempotency, per-user-limit, multi-seat); AI generated the boilerplate async gather code |
| WRITEUP | Outlined the key design decisions myself; used AI to improve phrasing and structure |
| Logging setup | Designed the per-startup timestamped file pattern; AI generated the RotatingFileHandler wiring |

---

## 7. What I'd Do Next

1. **JWT authentication** — replace the Bearer-token-as-user-id shortcut with OIDC/JWT validation.
2. **Timed holds** — add a `held_until TIMESTAMPTZ` column and a background cleanup task to support a hold→pay→confirm flow.
3. **Read replica** — route `GET /shows` to a read replica to reduce load on the primary during burst reads.
4. **Rate limiting** — per-IP or per-user request throttling to prevent a single client from monopolising the connection pool.
5. **Alerting + dashboards** — Grafana dashboard with the key counters above; PagerDuty for the P0 conditions.
6. **Larger load tests** — targeting more than 20,000 RPS to validate the Postgres connection pool sizing.

---

## 8. Live URL

> **URL** https://seat-reservation-zzba.onrender.com/

**To run locally from a clean checkout:**
```bash
docker compose up --build
# then in a second terminal:
make burst                    # 20,000-request stampede
make cancel-race              # concurrent cancellation test
make multi-race               # overlapping multi-seat race
make test                     # pytest integration suite
```
