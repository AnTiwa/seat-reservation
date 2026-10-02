import asyncio
import collections
import sys
import uuid

import httpx


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


async def main():
    async with httpx.AsyncClient(
        timeout=30,
        limits=httpx.Limits(
            max_connections=100,
            max_keepalive_connections=100,
        ),
    ) as client:

        # --------------------------------------------------
        # 1. Create a show with ONE seat
        # --------------------------------------------------

        response = await client.post(
            f"{BASE_URL}/shows",
            json={
                "name": "cancellation-race",
                "seats": ["A1"],
                "price_paise": 25000,
                "per_user_limit": 4,
            },
        )

        response.raise_for_status()

        show = response.json()
        show_id = show["id"]

        print(f"show={show_id}")

        # --------------------------------------------------
        # 2. Create one confirmed reservation
        # --------------------------------------------------

        response = await client.post(
            f"{BASE_URL}/shows/{show_id}/reserve",
            headers={
                "Authorization": "Bearer cancel-user",
                "Idempotency-Key": str(uuid.uuid4()),
            },
            json={
                "seats": ["A1"],
            },
        )

        response.raise_for_status()

        reservation = response.json()
        reservation_id = reservation["reservation_id"]

        print(f"reservation={reservation_id}")
        print(f"initial status={reservation['status']}")

        assert reservation["status"] == "confirmed"

        # --------------------------------------------------
        # 3. Confirm initial state
        # --------------------------------------------------

        response = await client.get(
            f"{BASE_URL}/shows/{show_id}"
        )

        response.raise_for_status()

        before = response.json()

        print("\n=== BEFORE CANCELLATION ===")
        print(before)

        assert before["counts"]["available"] == 0
        assert before["counts"]["confirmed"] == 1

        # --------------------------------------------------
        # 4. Race 100 cancellation requests
        # --------------------------------------------------

        async def cancel():
            response = await client.post(
                f"{BASE_URL}/reservations/{reservation_id}/cancel",
                headers={
                    "Authorization": "Bearer cancel-user",
                },
            )

            try:
                body = response.json()
            except Exception:
                body = {}

            return response.status_code, body

        results = await asyncio.gather(
            *(cancel() for _ in range(100))
        )

        # --------------------------------------------------
        # 5. Examine results
        # --------------------------------------------------

        print("\n=== RESULTS ===")

        status_counts = collections.Counter(
            status
            for status, body in results
        )

        print("status codes:", status_counts)

        for status, body in results:
            if status != 200:
                print(status, body)

        # Every request should succeed because your endpoint
        # treats an already-cancelled reservation as idempotent.
        assert all(
            status == 200
            for status, body in results
        )

        # Every response should report cancelled.
        assert all(
            body.get("status") == "cancelled"
            for status, body in results
        )

        # --------------------------------------------------
        # 6. Check final show state
        # --------------------------------------------------

        response = await client.get(
            f"{BASE_URL}/shows/{show_id}"
        )

        response.raise_for_status()

        final = response.json()

        print("\n=== FINAL STATE ===")
        print(final)

        counts = final["counts"]

        # Reconciliation invariant.
        assert (
            counts["available"]
            + counts["held"]
            + counts["confirmed"]
            == final["total_seats"]
        )

        # The one seat must have been released exactly once.
        assert counts["available"] == 1
        assert counts["held"] == 0
        assert counts["confirmed"] == 0

        # --------------------------------------------------
        # 7. Verify the reservation itself is cancelled
        # --------------------------------------------------

        # There isn't a GET /reservations/{id} endpoint in the
        # code you provided, so final state is verified through
        # /shows/{show_id}.

        print("\nCANCELLATION RACE: PASS")


if __name__ == "__main__":
    asyncio.run(main())
