import asyncio
import collections
import sys
import time
import uuid

import httpx


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"
TOTAL_REQUESTS = int(sys.argv[2]) if len(sys.argv) > 2 else 20_000
CONCURRENCY = 1_000


async def create_show(client, seats, name="burst-test"):
    response = await client.post(
        f"{BASE_URL}/shows",
        json={
            "name": name,
            "seats": seats,
            "price_paise": 25000,
            "per_user_limit": 4,
        },
    )

    response.raise_for_status()
    return response.json()


async def reserve(
    client,
    show_id,
    user_id,
    seats,
    key=None,
):
    if key is None:
        key = str(uuid.uuid4())

    try:
        response = await client.post(
            f"{BASE_URL}/shows/{show_id}/reserve",
            headers={
                "Authorization": f"Bearer {user_id}",
                "Idempotency-Key": key,
            },
            json={
                "seats": seats,
            },
        )

        try:
            body = response.json()
        except Exception:
            body = {}

        return response.status_code, body

    except Exception as exc:
        return 599, {
            "detail": f"client-error: {type(exc).__name__}: {exc}"
        }


def classify_response(status_code, body):
    if status_code == 201:
        return "confirmed"

    if status_code == 409:
        detail = body.get("detail")

        if isinstance(detail, dict):
            return detail.get("reason", "409")

        if isinstance(detail, str):
            if "limit" in detail:
                return "per-user-limit"

            if "idempotency" in detail:
                return "idempotent-replay"

            if "seat" in detail:
                return "seat-taken"

        return "409"

    if status_code >= 500:
        return f"5xx:{status_code}"

    return f"other:{status_code}"


async def bounded_gather(coros, concurrency=CONCURRENCY):
    semaphore = asyncio.Semaphore(concurrency)

    async def run(coro):
        async with semaphore:
            return await coro

    return await asyncio.gather(
        *(run(coro) for coro in coros)
    )


async def hot_seat_storm(client):
    print("\n=== HOT SEAT STORM ===")

    seats = [f"A{i}" for i in range(1, 101)]

    show = await create_show(
        client,
        seats,
        name="20k-hot-seat",
    )

    show_id = show["id"]

    print(f"show={show_id}")
    print(f"requests={TOTAL_REQUESTS}")
    print(f"concurrency={CONCURRENCY}")
    print("target=A1")

    start = time.perf_counter()

    coros = [
        reserve(
            client,
            show_id,
            f"hot-user-{i}",
            ["A1"],
        )
        for i in range(TOTAL_REQUESTS)
    ]

    results = await bounded_gather(coros)

    elapsed = time.perf_counter() - start

    outcomes = collections.Counter(
        classify_response(status, body)
        for status, body in results
    )

    print("outcomes:", outcomes)
    print(f"elapsed={elapsed:.2f}s")
    print(f"requests/sec={TOTAL_REQUESTS / elapsed:.2f}")

    confirmed = outcomes["confirmed"]
    five_xx = sum(
        count
        for name, count in outcomes.items()
        if name.startswith("5xx:")
    )

    assert confirmed == 1, (
        f"expected exactly one winner, got {confirmed}"
    )

    assert five_xx == 0, (
        f"expected zero 5xx, got {five_xx}"
    )

    final = await get_show(client, show_id)

    assert_reconciliation(final)

    assert final["counts"]["confirmed"] == 1

    a1 = next(
        seat
        for seat in final["seats"]
        if seat["seat_number"] == "A1"
    )

    assert a1["status"] == "confirmed"

    print("HOT SEAT: PASS")

    return show_id


async def idempotency_concurrency_test(client):
    print("\n=== IDEMPOTENCY CONCURRENCY ===")

    show = await create_show(
        client,
        ["A1", "A2", "A3", "A4", "A5"],
        name="idempotency-burst",
    )

    show_id = show["id"]

    key = str(uuid.uuid4())
    user = "retry-user"

    coros = [
        reserve(
            client,
            show_id,
            user,
            ["A1"],
            key=key,
        )
        for _ in range(100)
    ]

    results = await bounded_gather(coros)

    reservation_ids = [
        body.get("reservation_id")
        for status, body in results
        if status == 201
    ]

    outcomes = collections.Counter(
        classify_response(status, body)
        for status, body in results
    )

    print("outcomes:", outcomes)
    print("201 responses:", len(reservation_ids))
    print("unique reservation IDs:", len(set(reservation_ids)))

    assert len(results) == 100
    assert len(reservation_ids) == 100
    assert len(set(reservation_ids)) == 1

    final = await get_show(client, show_id)

    assert_reconciliation(final)
    assert final["counts"]["confirmed"] == 1

    print("IDEMPOTENCY: PASS")


async def per_user_limit_test(client):
    print("\n=== PER-USER LIMIT ===")

    seats = [f"A{i}" for i in range(1, 21)]

    show = await create_show(
        client,
        seats,
        name="per-user-limit-burst",
    )

    show_id = show["id"]
    user = "limited-user"

    coros = [
        reserve(
            client,
            show_id,
            user,
            [seat],
        )
        for seat in seats
    ]

    results = await bounded_gather(coros)

    outcomes = collections.Counter(
        classify_response(status, body)
        for status, body in results
    )

    print("outcomes:", outcomes)

    confirmed = outcomes["confirmed"]
    five_xx = sum(
        count
        for name, count in outcomes.items()
        if name.startswith("5xx:")
    )

    assert confirmed == 4, (
        f"expected exactly 4 confirmations, got {confirmed}"
    )

    assert five_xx == 0, (
        f"expected zero 5xx, got {five_xx}"
    )

    final = await get_show(client, show_id)

    assert_reconciliation(final)
    assert final["counts"]["confirmed"] == 4

    print("PER-USER LIMIT: PASS")


async def multi_seat_race_test(client):
    print("\n=== MULTI-SEAT RACE ===")

    show = await create_show(
        client,
        ["A1", "A2", "A3"],
        name="multi-seat-race-burst",
    )

    show_id = show["id"]

    coros = [
        reserve(
            client,
            show_id,
            "alice",
            ["A1", "A2"],
            key="alice-multi",
        ),
        reserve(
            client,
            show_id,
            "bob",
            ["A2", "A3"],
            key="bob-multi",
        ),
    ]

    results = await asyncio.gather(*coros)

    outcomes = [
        (
            user,
            classify_response(status, body),
            status,
            body,
        )
        for user, (status, body) in zip(
            ["alice", "bob"],
            results,
        )
    ]

    for outcome in outcomes:
        print(outcome)

    statuses = [status for _, _, status, _ in outcomes]

    assert sorted(statuses) == [201, 409]

    final = await get_show(client, show_id)

    assert_reconciliation(final)
    assert final["counts"]["confirmed"] == 2

    print("MULTI-SEAT: PASS")


async def get_show(client, show_id):
    response = await client.get(
        f"{BASE_URL}/shows/{show_id}"
    )

    response.raise_for_status()

    return response.json()


def assert_reconciliation(state):
    counts = state["counts"]

    total_from_counts = (
        counts["available"]
        + counts["held"]
        + counts["confirmed"]
    )

    assert total_from_counts == state["total_seats"], (
        "RECONCILIATION FAILED: "
        f"available={counts['available']} "
        f"held={counts['held']} "
        f"confirmed={counts['confirmed']} "
        f"total={state['total_seats']}"
    )


async def main():
    limits = httpx.Limits(
        max_connections=CONCURRENCY,
        max_keepalive_connections=100,
    )

    timeout = httpx.Timeout(
        connect=10,
        read=60,
        write=60,
        pool=60,
    )

    async with httpx.AsyncClient(
        timeout=timeout,
        limits=limits,
    ) as client:

        overall_start = time.perf_counter()

        await hot_seat_storm(client)

        await idempotency_concurrency_test(client)

        await per_user_limit_test(client)

        await multi_seat_race_test(client)

        elapsed = time.perf_counter() - overall_start

        print("\n========================================")
        print("ALL BURST TESTS: PASS")
        print(f"elapsed={elapsed:.2f}s")
        print("========================================")


if __name__ == "__main__":
    asyncio.run(main())
