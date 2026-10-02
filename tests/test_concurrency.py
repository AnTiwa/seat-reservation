"""
test_concurrency.py — Integration tests for correctness under concurrency.

Requires a running service (docker compose up --build) and is driven by
the pytest fixtures defined in conftest.py.

Run with:
    pytest tests/test_concurrency.py -v
"""
import asyncio
import uuid

import pytest


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


async def create_show(client, seats, price=100, limit=4, name="test"):
    resp = await client.post(
        "/shows",
        json={
            "name": name,
            "seats": seats,
            "price_paise": price,
            "per_user_limit": limit,
        },
    )
    assert resp.status_code == 201, resp.text
    return resp.json()


async def reserve(client, show_id, user_id, seats, key=None):
    if key is None:
        key = str(uuid.uuid4())
    resp = await client.post(
        f"/shows/{show_id}/reserve",
        headers={
            "Authorization": f"Bearer {user_id}",
            "Idempotency-Key": key,
        },
        json={"seats": seats},
    )
    return resp


def assert_reconciliation(state):
    counts = state["counts"]
    total = counts["available"] + counts["held"] + counts["confirmed"]
    assert total == state["total_seats"], (
        f"Reconciliation failed: {counts}, total_seats={state['total_seats']}"
    )


# ---------------------------------------------------------------------------
# Test: Hot seat — exactly one winner
# ---------------------------------------------------------------------------


async def test_hot_seat_exactly_one_winner(http_client):
    """
    500 users race for a single seat.
    Exactly 1 must be confirmed, 499 must receive 409, 0 must receive 5xx.
    """
    show = await create_show(http_client, ["A1"], name="hot-seat-test")
    show_id = show["id"]

    responses = await asyncio.gather(
        *[
            reserve(http_client, show_id, f"user-{i}", ["A1"])
            for i in range(500)
        ]
    )

    status_codes = [r.status_code for r in responses]

    assert status_codes.count(201) == 1, (
        f"Expected exactly 1 confirmed, got {status_codes.count(201)}"
    )
    assert status_codes.count(409) == 499, (
        f"Expected 499 declined, got {status_codes.count(409)}"
    )
    assert not any(c >= 500 for c in status_codes), (
        f"Got 5xx responses: {[c for c in status_codes if c >= 500]}"
    )

    final = await http_client.get(f"/shows/{show_id}")
    state = final.json()
    assert_reconciliation(state)
    assert state["counts"]["confirmed"] == 1
    assert state["counts"]["available"] == 0


# ---------------------------------------------------------------------------
# Test: Per-user seat limit enforced under concurrency
# ---------------------------------------------------------------------------


async def test_user_limit_under_concurrency(http_client):
    """
    A single user fires 10 parallel reservations on a limit=4 show.
    At most 4 must be confirmed, the rest must be 409.
    """
    seats = [f"B{i}" for i in range(1, 11)]
    show = await create_show(
        http_client, seats, limit=4, name="limit-test"
    )
    show_id = show["id"]

    responses = await asyncio.gather(
        *[
            reserve(http_client, show_id, "same-user", [f"B{i}"])
            for i in range(1, 11)
        ]
    )

    confirmed = sum(1 for r in responses if r.status_code == 201)
    assert confirmed == 4, f"Expected 4 confirmed, got {confirmed}"
    assert all(
        r.status_code in (201, 409) for r in responses
    ), "Got unexpected status codes"

    final = await http_client.get(f"/shows/{show_id}")
    assert_reconciliation(final.json())


# ---------------------------------------------------------------------------
# Test: Idempotency — same key returns same reservation
# ---------------------------------------------------------------------------


async def test_idempotency_concurrent_retries(http_client):
    """
    100 concurrent requests with the same idempotency key for the same user
    must all return 201 with the *same* reservation_id.
    """
    show = await create_show(
        http_client,
        ["C1", "C2"],
        name="idempotency-test",
    )
    show_id = show["id"]
    shared_key = str(uuid.uuid4())

    responses = await asyncio.gather(
        *[
            reserve(http_client, show_id, "retry-user", ["C1"], key=shared_key)
            for _ in range(100)
        ]
    )

    assert all(r.status_code == 201 for r in responses), (
        f"Not all 201: {[r.status_code for r in responses if r.status_code != 201]}"
    )

    reservation_ids = {r.json()["reservation_id"] for r in responses}
    assert len(reservation_ids) == 1, (
        f"Expected 1 unique reservation_id, got {len(reservation_ids)}"
    )

    final = await http_client.get(f"/shows/{show_id}")
    state = final.json()
    assert_reconciliation(state)
    assert state["counts"]["confirmed"] == 1


# ---------------------------------------------------------------------------
# Test: Different body on same idempotency key → 409
# ---------------------------------------------------------------------------


async def test_idempotency_key_body_mismatch(http_client):
    """
    Reusing an idempotency key with a different seat set must return 409.
    """
    show = await create_show(
        http_client,
        ["D1", "D2"],
        name="idemp-mismatch",
    )
    show_id = show["id"]
    key = str(uuid.uuid4())

    r1 = await reserve(http_client, show_id, "mismatch-user", ["D1"], key=key)
    assert r1.status_code == 201

    r2 = await reserve(http_client, show_id, "mismatch-user", ["D2"], key=key)
    assert r2.status_code == 409, f"Expected 409, got {r2.status_code}: {r2.text}"
    assert "different" in r2.json()["detail"].lower()


# ---------------------------------------------------------------------------
# Test: Multi-seat all-or-nothing under concurrency
# ---------------------------------------------------------------------------


async def test_multi_seat_all_or_nothing(http_client):
    """
    Two users race for overlapping seats [A1,A2] vs [A2,A3].
    Exactly one wins (201), the other loses (409), and reconciliation holds.
    """
    show = await create_show(
        http_client,
        ["E1", "E2", "E3"],
        name="multi-seat-test",
    )
    show_id = show["id"]

    alice_resp, bob_resp = await asyncio.gather(
        reserve(http_client, show_id, "alice", ["E1", "E2"], key="alice-e"),
        reserve(http_client, show_id, "bob",   ["E2", "E3"], key="bob-e"),
    )

    statuses = sorted([alice_resp.status_code, bob_resp.status_code])
    assert statuses == [201, 409], f"Expected [201, 409], got {statuses}"

    final = await http_client.get(f"/shows/{show_id}")
    state = final.json()
    assert_reconciliation(state)
    assert state["counts"]["confirmed"] == 2


# ---------------------------------------------------------------------------
# Test: Cancellation is idempotent under concurrency
# ---------------------------------------------------------------------------


async def test_cancel_idempotent_under_concurrency(http_client):
    """
    100 concurrent cancel requests for the same reservation must all
    return 200 and leave the seat as 'available'.
    """
    show = await create_show(
        http_client, ["F1"], name="cancel-race-test"
    )
    show_id = show["id"]

    r = await reserve(http_client, show_id, "cancel-user", ["F1"])
    assert r.status_code == 201
    reservation_id = r.json()["reservation_id"]

    cancels = await asyncio.gather(
        *[
            http_client.post(
                f"/reservations/{reservation_id}/cancel",
                headers={"Authorization": "Bearer cancel-user"},
            )
            for _ in range(100)
        ]
    )

    assert all(c.status_code == 200 for c in cancels), (
        f"Non-200 cancels: {[(c.status_code, c.text) for c in cancels if c.status_code != 200]}"
    )
    assert all(c.json()["status"] == "cancelled" for c in cancels)

    final = await http_client.get(f"/shows/{show_id}")
    state = final.json()
    assert_reconciliation(state)
    assert state["counts"]["available"] == 1
    assert state["counts"]["confirmed"] == 0


# ---------------------------------------------------------------------------
# Test: Health endpoints
# ---------------------------------------------------------------------------


async def test_liveness(http_client):
    resp = await http_client.get("/health/live")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


async def test_readiness(http_client):
    resp = await http_client.get("/health/ready")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ready"


# ---------------------------------------------------------------------------
# Test: Metrics endpoint is reachable and contains expected metric names
# ---------------------------------------------------------------------------


async def test_metrics_endpoint(http_client):
    resp = await http_client.get("/metrics/")
    assert resp.status_code == 200
    text = resp.text
    assert "reservations_confirmed_total" in text
    assert "reservations_declined_total" in text
    assert "seats_available" in text
    assert "http_request_duration_seconds" in text
