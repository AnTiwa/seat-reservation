import hashlib
import json
import logging
import time
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from prometheus_client import make_asgi_app

from app.auth import get_current_user
from app.db import close_pool, init_pool, init_schema, pool
from app.logging_config import configure_logging
from app.metrics import (
    http_request_duration_seconds,
    request_errors,
    reservations_confirmed,
    reservations_declined,
    seats_available,
)

# Configure structured JSON logging to stdout + timestamped file.
configure_logging()

logger = logging.getLogger("seat-service")


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_pool()
    init_schema()
    yield
    close_pool()


app = FastAPI(
    title="Seat Reservation Service",
    version="1.0.0",
    lifespan=lifespan,
)

app.mount("/metrics", make_asgi_app())


class CreateShowRequest(BaseModel):
    name: str = Field(min_length=1)
    seats: list[str] = Field(min_length=1)
    price_paise: int = Field(ge=0)
    per_user_limit: int = Field(default=4, ge=1)


class ReserveRequest(BaseModel):
    seats: list[str] = Field(min_length=1)


class CancelResponse(BaseModel):
    reservation_id: str
    status: str


def request_hash(seats: list[str]) -> str:
    normalized = sorted(set(seats))

    payload = json.dumps(
        {
            "seats": normalized,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    return hashlib.sha256(payload.encode()).hexdigest()


def reservation_response(conn, reservation_id):
    row = conn.execute(
        """
        SELECT
            r.id,
            r.show_id,
            r.user_id,
            r.amount_paise,
            r.status,
            COALESCE(
                json_agg(rs.seat_number ORDER BY rs.seat_number)
                    FILTER (WHERE rs.seat_number IS NOT NULL),
                '[]'
            ) AS seats
        FROM reservations r
        LEFT JOIN reservation_seats rs
            ON rs.reservation_id = r.id
        WHERE r.id = %s
        GROUP BY r.id
        """,
        (reservation_id,),
    ).fetchone()

    if not row:
        raise HTTPException(
            status_code=404,
            detail="reservation not found",
        )

    return {
        "reservation_id": str(row[0]),
        "show_id": str(row[1]),
        "user_id": row[2],
        "seats": row[5],
        "amount_paise": row[3],
        "status": row[4],
    }


@app.middleware("http")
async def request_logging(request: Request, call_next):
    correlation_id = request.headers.get(
        "X-Request-ID",
        str(uuid.uuid4()),
    )

    start = time.perf_counter()

    try:
        response = await call_next(request)
    except Exception:
        request_errors.inc()

        logger.exception(
            json.dumps(
                {
                    "event": "unhandled_exception",
                    "request_id": correlation_id,
                    "method": request.method,
                    "path": request.url.path,
                }
            )
        )

        raise

    elapsed = time.perf_counter() - start

    # Collapse path parameters so cardinality stays low in Prometheus.
    path_template = request.scope.get("route") and request.scope["route"].path or request.url.path
    status_class = f"{response.status_code // 100}xx"

    http_request_duration_seconds.labels(
        method=request.method,
        path_template=path_template,
        status_class=status_class,
    ).observe(elapsed)

    response.headers["X-Request-ID"] = correlation_id

    logger.info(
        json.dumps(
            {
                "event": "request",
                "request_id": correlation_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round(elapsed * 1000, 2),
            }
        )
    )

    if response.status_code >= 500:
        request_errors.inc()

    return response


@app.get("/health/live")
def liveness():
    return {
        "status": "ok",
    }


@app.get("/health/ready")
def readiness():
    try:
        with pool.connection() as conn:
            conn.execute("SELECT 1")
        return {
            "status": "ready",
        }
    except Exception:
        return JSONResponse(
            status_code=503,
            content={
                "status": "not_ready",
            },
        )


@app.post("/shows", status_code=201)
def create_show(body: CreateShowRequest):
    if len(body.seats) != len(set(body.seats)):
        raise HTTPException(
            status_code=400,
            detail="duplicate seat numbers",
        )

    show_id = uuid.uuid4()

    with pool.connection() as conn:
        with conn.transaction():
            conn.execute(
                """
                INSERT INTO shows (
                    id,
                    name,
                    price_paise,
                    per_user_limit
                )
                VALUES (%s, %s, %s, %s)
                """,
                (
                    show_id,
                    body.name,
                    body.price_paise,
                    body.per_user_limit,
                ),
            )

            with conn.cursor() as cur:
                cur.executemany(
                    """
                    INSERT INTO seats (
                        show_id,
                        seat_number,
                        status
                    )
                    VALUES (%s, %s, 'available')
                    """,
                    [
                        (show_id, seat)
                        for seat in body.seats
                    ],
                )


    seats_available.labels(str(show_id)).set(len(body.seats))

    return {
        "id": str(show_id),
        "name": body.name,
        "seats": [
            {
                "seat_number": seat,
                "status": "available",
            }
            for seat in body.seats
        ],
        "price_paise": body.price_paise,
        "per_user_limit": body.per_user_limit,
    }


@app.get("/shows/{show_id}")
def get_show(show_id: str):
    try:
        show_uuid = uuid.UUID(show_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail="show not found",
        )

    with pool.connection() as conn:
        show = conn.execute(
            """
            SELECT
                id,
                name,
                price_paise,
                per_user_limit
            FROM shows
            WHERE id = %s
            """,
            (show_uuid,),
        ).fetchone()

        if not show:
            raise HTTPException(
                status_code=404,
                detail="show not found",
            )

        rows = conn.execute(
            """
            SELECT seat_number, status
            FROM seats
            WHERE show_id = %s
            ORDER BY seat_number
            """,
            (show_uuid,),
        ).fetchall()

    counts = {
        "available": 0,
        "held": 0,
        "confirmed": 0,
    }

    seat_data = []

    for seat_number, status in rows:
        counts[status] += 1

        seat_data.append(
            {
                "seat_number": seat_number,
                "status": status,
            }
        )

    total = len(rows)

    assert (
        counts["available"]
        + counts["held"]
        + counts["confirmed"]
        == total
    )

    seats_available.labels(str(show_uuid)).set(
        counts["available"]
    )

    return {
        "id": str(show[0]),
        "name": show[1],
        "price_paise": show[2],
        "per_user_limit": show[3],
        "total_seats": total,
        "counts": counts,
        "seats": seat_data,
    }


@app.post("/shows/{show_id}/reserve", status_code=201)
def reserve(
    show_id: str,
    body: ReserveRequest,
    authorization: str | None = Header(default=None),
    idempotency_key: str | None = Header(default=None),
):
    user_id = get_current_user(authorization)

    if not idempotency_key:
        raise HTTPException(
            status_code=400,
            detail="Idempotency-Key header is required",
        )

    try:
        show_uuid = uuid.UUID(show_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail="show not found",
        )

    if len(body.seats) != len(set(body.seats)):
        reservations_declined.labels("invalid-request").inc()

        raise HTTPException(
            status_code=400,
            detail="duplicate seats in request",
        )

    requested_seats = sorted(body.seats)
    body_hash = request_hash(requested_seats)

    with pool.connection() as conn:
        try:
            with conn.transaction():
                # --------------------------------------------------
                # 1. Idempotency row.
                #
                # The unique PK guarantees one key can only have
                # one logical operation for this show.
                # --------------------------------------------------

                inserted = conn.execute(
                    """
                    INSERT INTO idempotency_keys (
                        show_id,
                        idempotency_key,
                        user_id,
                        request_hash
                    )
                    VALUES (%s, %s, %s, %s)
                    ON CONFLICT DO NOTHING
                    RETURNING
                        reservation_id,
                        user_id,
                        request_hash
                    """,
                    (
                        show_uuid,
                        idempotency_key,
                        user_id,
                        body_hash,
                    ),
                ).fetchone()

                if inserted is None:
                    existing = conn.execute(
                        """
                        SELECT
                            reservation_id,
                            user_id,
                            request_hash
                        FROM idempotency_keys
                        WHERE show_id = %s
                          AND idempotency_key = %s
                        FOR UPDATE
                        """,
                        (
                            show_uuid,
                            idempotency_key,
                        ),
                    ).fetchone()

                    if existing is None:
                        raise HTTPException(
                            status_code=409,
                            detail="idempotency conflict",
                        )

                    existing_reservation_id = existing[0]
                    existing_user_id = existing[1]
                    existing_hash = existing[2]

                    if (
                        existing_user_id != user_id
                        or existing_hash != body_hash
                    ):
                        reservations_declined.labels(
                            "idempotent-replay"
                        ).inc()

                        raise HTTPException(
                            status_code=409,
                            detail=(
                                "idempotency key already used "
                                "with different request"
                            ),
                        )

                    if existing_reservation_id is None:
                        # Another transaction has inserted the key but
                        # hasn't completed yet. FOR UPDATE above means
                        # we wait until it commits, so normally this
                        # branch is not reachable.
                        raise HTTPException(
                            status_code=409,
                            detail="idempotency operation incomplete",
                        )

                    reservations_declined.labels(
                        "idempotent-replay"
                    ).inc()

                    return reservation_response(
                        conn,
                        existing_reservation_id,
                    )

                # --------------------------------------------------
                # 2. Load show.
                # --------------------------------------------------

                show = conn.execute(
                    """
                    SELECT
                        price_paise,
                        per_user_limit
                    FROM shows
                    WHERE id = %s
                    FOR SHARE
                    """,
                    (show_uuid,),
                ).fetchone()

                if not show:
                    raise HTTPException(
                        status_code=404,
                        detail="show not found",
                    )

                price_paise, per_user_limit = show

                # --------------------------------------------------
                # 3. Serialize all requests for the same user/show.
                # --------------------------------------------------

                conn.execute(
                    """
                    INSERT INTO user_show_counters (
                        show_id,
                        user_id,
                        seat_count
                    )
                    VALUES (%s, %s, 0)
                    ON CONFLICT DO NOTHING
                    """,
                    (
                        show_uuid,
                        user_id,
                    ),
                )

                counter = conn.execute(
                    """
                    SELECT seat_count
                    FROM user_show_counters
                    WHERE show_id = %s
                      AND user_id = %s
                    FOR UPDATE
                    """,
                    (
                        show_uuid,
                        user_id,
                    ),
                ).fetchone()

                current_count = counter[0]

                if current_count + len(requested_seats) > per_user_limit:
                    reservations_declined.labels(
                        "per-user-limit"
                    ).inc()

                    raise HTTPException(
                        status_code=409,
                        detail="per-user seat limit exceeded",
                    )

                # --------------------------------------------------
                # 4. Lock requested seats in deterministic order.
                #
                # This prevents A+B vs B+A deadlocks.
                # --------------------------------------------------

                seat_rows = conn.execute(
                    """
                    SELECT
                        seat_number,
                        status
                    FROM seats
                    WHERE show_id = %s
                      AND seat_number = ANY(%s)
                    ORDER BY seat_number
                    FOR UPDATE
                    """,
                    (
                        show_uuid,
                        requested_seats,
                    ),
                ).fetchall()

                if len(seat_rows) != len(requested_seats):
                    reservations_declined.labels(
                        "seat-not-found"
                    ).inc()

                    raise HTTPException(
                        status_code=409,
                        detail="one or more seats do not exist",
                    )

                unavailable = [
                    seat_number
                    for seat_number, status in seat_rows
                    if status != "available"
                ]

                if unavailable:
                    reservations_declined.labels(
                        "seat-taken"
                    ).inc()

                    raise HTTPException(
                        status_code=409,
                        detail={
                            "reason": "seat-taken",
                            "seats": unavailable,
                        },
                    )
                # --------------------------------------------------
                # 5. Create reservation parent row FIRST.
                # --------------------------------------------------

                reservation_id = uuid.uuid4()
                amount = price_paise * len(requested_seats)

                conn.execute(
                    """
                    INSERT INTO reservations (
                        id,
                        show_id,
                        user_id,
                        amount_paise,
                        status
                    )
                    VALUES (%s, %s, %s, %s, 'confirmed')
                    """,
                    (
                        reservation_id,
                        show_uuid,
                        user_id,
                        amount,
                    ),
                )

                # --------------------------------------------------
                # 6. Create reservation -> seat records.
                # --------------------------------------------------

                with conn.cursor() as cur:
                    cur.executemany(
                        """
                        INSERT INTO reservation_seats (
                            reservation_id,
                            show_id,
                            seat_number
                        )
                        VALUES (%s, %s, %s)
                        """,
                        [
                            (
                                reservation_id,
                                show_uuid,
                                seat,
                            )
                            for seat in requested_seats
                        ],
                    )

                # --------------------------------------------------
                # 7. Transition the locked seats to confirmed.
                #
                # The seats were already SELECT ... FOR UPDATE'd
                # above, so another reservation cannot modify them
                # while this transaction is active.
                # --------------------------------------------------

                updated = conn.execute(
                    """
                    UPDATE seats
                    SET
                        status = 'confirmed',
                        reservation_id = %s,
                        updated_at = now()
                    WHERE show_id = %s
                      AND seat_number = ANY(%s)
                      AND status = 'available'
                    """,
                    (
                        reservation_id,
                        show_uuid,
                        requested_seats,
                    ),
                ).rowcount

                if updated != len(requested_seats):
                    raise HTTPException(
                        status_code=409,
                        detail="seat became unavailable",
                    )

                # --------------------------------------------------
                # 8. Update per-user seat count.
                # --------------------------------------------------

                conn.execute(
                    """
                    UPDATE user_show_counters
                    SET seat_count = seat_count + %s
                    WHERE show_id = %s
                      AND user_id = %s
                    """,
                    (
                        len(requested_seats),
                        show_uuid,
                        user_id,
                    ),
                )

                # --------------------------------------------------
                # 9. Complete idempotency record.
                # --------------------------------------------------

                conn.execute(
                    """
                    UPDATE idempotency_keys
                    SET reservation_id = %s
                    WHERE show_id = %s
                      AND idempotency_key = %s
                    """,
                    (
                        reservation_id,
                        show_uuid,
                        idempotency_key,
                    ),
                )

            reservations_confirmed.inc()

            # Update gauge after successful commit.
            with conn.transaction():
                available = conn.execute(
                    """
                    SELECT COUNT(*)
                    FROM seats
                    WHERE show_id = %s
                      AND status = 'available'
                    """,
                    (show_uuid,),
                ).fetchone()[0]

            seats_available.labels(str(show_uuid)).set(
                available
            )

            return reservation_response(
                conn,
                reservation_id,
            )

        except HTTPException:
            raise
        except Exception:
            logger.exception(
                json.dumps(
                    {
                        "event": "reservation_failure",
                        "show_id": show_id,
                        "user_id": user_id,
                    }
                )
            )
            raise


@app.post(
    "/reservations/{reservation_id}/cancel",
    response_model=CancelResponse,
)
def cancel_reservation(
    reservation_id: str,
    authorization: str | None = Header(default=None),
):
    user_id = get_current_user(authorization)

    try:
        reservation_uuid = uuid.UUID(reservation_id)
    except ValueError:
        raise HTTPException(
            status_code=404,
            detail="reservation not found",
        )

    with pool.connection() as conn:
        with conn.transaction():

            # ------------------------------------------------------
            # First get the reservation WITHOUT locking it.
            #
            # We need show_id/user_id so we can acquire the locks
            # in exactly the same order as reserve():
            #
            #   user_show_counter -> reservation -> seats
            # ------------------------------------------------------

            reservation = conn.execute(
                """
                SELECT
                    id,
                    show_id,
                    user_id,
                    status
                FROM reservations
                WHERE id = %s
                """,
                (reservation_uuid,),
            ).fetchone()

            if not reservation:
                raise HTTPException(
                    status_code=404,
                    detail="reservation not found",
                )

            _, show_id, owner_id, initial_status = reservation

            if owner_id != user_id:
                raise HTTPException(
                    status_code=403,
                    detail="not reservation owner",
                )

            # ------------------------------------------------------
            # Same lock order as reservation().
            # ------------------------------------------------------

            conn.execute(
                """
                INSERT INTO user_show_counters (
                    show_id,
                    user_id,
                    seat_count
                )
                VALUES (%s, %s, 0)
                ON CONFLICT DO NOTHING
                """,
                (
                    show_id,
                    user_id,
                ),
            )

            conn.execute(
                """
                SELECT seat_count
                FROM user_show_counters
                WHERE show_id = %s
                  AND user_id = %s
                FOR UPDATE
                """,
                (
                    show_id,
                    user_id,
                ),
            )

            # Now lock the reservation itself.
            reservation = conn.execute(
                """
                SELECT
                    id,
                    show_id,
                    user_id,
                    status
                FROM reservations
                WHERE id = %s
                FOR UPDATE
                """,
                (reservation_uuid,),
            ).fetchone()

            if not reservation:
                raise HTTPException(
                    status_code=404,
                    detail="reservation not found",
                )

            _, show_id, owner_id, status = reservation

            if owner_id != user_id:
                raise HTTPException(
                    status_code=403,
                    detail="not reservation owner",
                )

            if status == "cancelled":
                return {
                    "reservation_id": reservation_id,
                    "status": "cancelled",
                }

            # ------------------------------------------------------
            # Lock the reservation's seats in deterministic order.
            # ------------------------------------------------------

            seat_rows = conn.execute(
                """
                SELECT seat_number
                FROM reservation_seats
                WHERE reservation_id = %s
                ORDER BY seat_number
                FOR UPDATE
                """,
                (reservation_uuid,),
            ).fetchall()

            seat_numbers = [
                row[0]
                for row in seat_rows
            ]

            # ------------------------------------------------------
            # Release seats.
            # ------------------------------------------------------

            conn.execute(
                """
                UPDATE seats
                SET
                    status = 'available',
                    reservation_id = NULL,
                    updated_at = now()
                WHERE reservation_id = %s
                """,
                (reservation_uuid,),
            )

            # ------------------------------------------------------
            # Cancel reservation.
            # ------------------------------------------------------

            conn.execute(
                """
                UPDATE reservations
                SET
                    status = 'cancelled',
                    cancelled_at = now()
                WHERE id = %s
                """,
                (reservation_uuid,),
            )

            # ------------------------------------------------------
            # Release user's seat count.
            # ------------------------------------------------------

            conn.execute(
                """
                UPDATE user_show_counters
                SET seat_count = GREATEST(
                    0,
                    seat_count - %s
                )
                WHERE show_id = %s
                  AND user_id = %s
                """,
                (
                    len(seat_numbers),
                    show_id,
                    user_id,
                ),
            )

        # Transaction committed here.

        available = conn.execute(
            """
            SELECT COUNT(*)
            FROM seats
            WHERE show_id = %s
              AND status = 'available'
            """,
            (show_id,),
        ).fetchone()[0]

        seats_available.labels(str(show_id)).set(
            available
        )

    return {
        "reservation_id": reservation_id,
        "status": "cancelled",
    }
