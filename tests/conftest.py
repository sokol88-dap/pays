import os
from collections.abc import AsyncGenerator

import httpx
import pytest
import pytest_asyncio
from pydantic import JsonValue
from sqlalchemy import text
from sqlalchemy.engine import make_url

from src.api import create_app
from src.config import Settings
from src.database import Database


@pytest.fixture
def settings() -> Settings:
    return Settings.model_validate({"api_key": "test-key", "_env_file": None})


@pytest.fixture
def payment_body() -> dict[str, JsonValue]:
    return {
        "amount": "125.50",
        "currency": "RUB",
        "description": "Order 42",
        "metadata": {"order_id": 42},
        "webhook_url": "https://merchant.example/webhooks/payments",
    }


@pytest_asyncio.fixture
async def database() -> AsyncGenerator[Database, None]:
    url = os.environ.get("TEST_DATABASE_URL")
    if not url:
        pytest.skip("Set TEST_DATABASE_URL to run PostgreSQL integration tests")
    if not (make_url(url).database or "").endswith("_test"):
        pytest.fail(
            "TEST_DATABASE_URL must name a dedicated database ending in _test"
        )

    database = Database(url)
    async with database.engine.begin() as connection:
        # The test database must already have been migrated with Alembic.
        await connection.execute(text("TRUNCATE outbox, payments"))
    yield database
    await database.close()


@pytest_asyncio.fixture
async def api_client(
    database: Database, settings: Settings
) -> AsyncGenerator[httpx.AsyncClient, None]:
    app = create_app(settings, database)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://test",
        headers={"X-API-Key": "test-key"},
    ) as client:
        yield client
