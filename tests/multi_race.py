"""
multi_race.py — Overlapping multi-seat reservation race test.

Creates a fresh show, then fires two concurrent reservations for
overlapping seats and verifies:
  - Exactly one wins (201) and the other loses (409).
  - Reconciliation invariant holds.

Usage:
    python tests/multi_race.py [BASE_URL]
"""
import asyncio
import sys

import httpx


BASE_URL = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8000"


async def reserve(client, user, seats, key, show_id):
    response = await client.post(
        f"{BASE_URL}/shows/{show_id}/reserve",
        headers={
            "Authorization": f"Bearer {user}",
            "Idempotency-Key": key,
        },
        json={"seats": seats},
    )
    try:
        body = response.json()
    except Exception:
        body = {}
    return user, response.status_code, body


async def main():
    async with httpx.AsyncClient(timeout=30) as client:
        # Create a fresh 3-seat show.
        resp = await client.post(
            f"{BASE_URL}/shows",
            json={
                "name": "multi-race",
                "seats": ["A1", "A2", "A3"],
                "price_paise": 25000,
                "per_user_limit": 4,
            },
        )
        resp.raise_for_status()
        show_id = resp.json()["id"]
        print(f"show={show_id}")

        # Fire alice (A1+A2) and bob (A2+A3) simultaneously.
        results = await asyncio.gather(
            reserve(client, "alice", ["A1", "A2"], "alice-multi", show_id),
            reserve(client, "bob",   ["A2", "A3"], "bob-multi",   show_id),
        )

        print("=== RESULTS ===")
        for user, status, body in results:
            print(f"  {user}: HTTP {status}  {body}")

        statuses = [status for _, status, _ in results]
        assert sorted(statuses) == [201, 409], (
            f"Expected [201, 409], got {statuses}"
        )

        # Verify reconciliation.
        final = await client.get(f"{BASE_URL}/shows/{show_id}")
        final.raise_for_status()
        state = final.json()
        counts = state["counts"]

        total = counts["available"] + counts["held"] + counts["confirmed"]
        assert total == state["total_seats"], (
            f"RECONCILIATION FAILED: {counts} total={state['total_seats']}"
        )

        # Winner reserved 2 seats.
        assert counts["confirmed"] == 2, (
            f"Expected 2 confirmed, got {counts['confirmed']}"
        )

        print(f"\n=== FINAL STATE ===\ncounts={counts}")
        print("\nMULTI-RACE: PASS")


if __name__ == "__main__":
    asyncio.run(main())
