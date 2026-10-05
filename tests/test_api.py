import asyncio
from typing import Never
from uuid import uuid4

import httpx
import pytest
from pydantic import JsonValue
from sqlalchemy import func, select

from src.database import Database
from src.models import OutboxEvent, Payment
from src.payments import create_payment
from src.schemas import PaymentCreate

pytestmark = pytest.mark.integration


async def test_create_and_read_payment(
    api_client: httpx.AsyncClient,
    database: Database,
    payment_body: dict[str, JsonValue],
) -> None:
    response = await api_client.post(
        "/api/v1/payments",
        json=payment_body,
        headers={"Idempotency-Key": "order-42"},
    )
    assert response.status_code == 202
    accepted = response.json()
    assert accepted["status"] == "pending"
    assert set(accepted) == {"payment_id", "status", "created_at"}

    details = await api_client.get(f"/api/v1/payments/{accepted['payment_id']}")
    assert details.status_code == 200
    assert details.json()["amount"] == "125.50"
    assert details.json()["metadata"] == {"order_id": 42}
    assert details.json()["processed_at"] is None
    async with database.sessions() as session:
        event = (await session.scalars(select(OutboxEvent))).one()
        assert str(event.payment_id) == accepted["payment_id"]
        assert event.payload["event_id"] == str(event.id)
        assert event.published_at is None


async def test_concurrent_duplicates_create_one_payment_and_one_event(
    api_client: httpx.AsyncClient,
    database: Database,
    payment_body: dict[str, JsonValue],
) -> None:
    responses = await asyncio.gather(
        *(
            api_client.post(
                "/api/v1/payments",
                json=payment_body,
                headers={"Idempotency-Key": "same-key"},
            )
            for _ in range(10)
        )
    )
    assert all(response.status_code == 202 for response in responses)
    assert len({response.json()["payment_id"] for response in responses}) == 1
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Payment)) == 1
        )
        assert (
            await session.scalar(select(func.count()).select_from(OutboxEvent))
            == 1
        )


async def test_changed_request_with_same_key_is_a_conflict(
    api_client: httpx.AsyncClient, payment_body: dict[str, JsonValue]
) -> None:
    headers = {"Idempotency-Key": "same-key"}
    await api_client.post(
        "/api/v1/payments", json=payment_body, headers=headers
    )
    response = await api_client.post(
        "/api/v1/payments",
        json={**payment_body, "amount": "150.00"},
        headers=headers,
    )
    assert response.status_code == 409


@pytest.mark.parametrize("key", [None, "", "   ", "a" * 256])
async def test_idempotency_key_is_required_and_validated(
    api_client: httpx.AsyncClient,
    payment_body: dict[str, JsonValue],
    key: str | None,
) -> None:
    headers = {} if key is None else {"Idempotency-Key": key}
    response = await api_client.post(
        "/api/v1/payments", json=payment_body, headers=headers
    )
    assert response.status_code == 422


async def test_missing_payment_returns_404(
    api_client: httpx.AsyncClient,
) -> None:
    response = await api_client.get(f"/api/v1/payments/{uuid4()}")
    assert response.status_code == 404


async def test_outbox_insert_failure_rolls_back_payment(
    database: Database,
    payment_body: dict[str, JsonValue],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def fail_event_creation(**kwargs: object) -> Never:
        raise RuntimeError("Unable to create event")

    monkeypatch.setattr("src.payments.OutboxEvent", fail_event_creation)
    async with database.sessions() as session:
        with pytest.raises(RuntimeError):
            await create_payment(
                session, PaymentCreate.model_validate(payment_body), "order-42"
            )
    async with database.sessions() as session:
        assert (
            await session.scalar(select(func.count()).select_from(Payment)) == 0
        )
        assert (
            await session.scalar(select(func.count()).select_from(OutboxEvent))
            == 0
        )
