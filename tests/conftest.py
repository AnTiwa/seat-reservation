"""
conftest.py — Shared pytest fixtures for the integration test suite.

Tests assume a running service at BASE_URL (default: http://localhost:8000).
Start the stack with ``docker compose up --build`` before running tests.
"""
import os

import pytest
import httpx


BASE_URL = os.environ.get("BASE_URL", "http://localhost:8000")


@pytest.fixture(scope="session")
def base_url() -> str:
    return BASE_URL


@pytest.fixture
async def http_client():
    """Async HTTP client with generous limits for concurrency tests."""
    async with httpx.AsyncClient(
        base_url=BASE_URL,
        timeout=60,
        limits=httpx.Limits(
            max_connections=1_000,
            max_keepalive_connections=200,
        ),
    ) as client:
        yield client
