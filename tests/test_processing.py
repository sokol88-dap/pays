from unittest.mock import AsyncMock

import httpx
import pytest
from pydantic import JsonValue
from sqlalchemy import select

from src.database import Database
from src.models import OutboxEvent, Payment
from src.payments import create_payment
from src.processing import (
    PaymentProcessor,
    WebhookAttemptsExhausted,
    WebhookDeliveryError,
)
from src.schemas import PaymentCreate, PaymentEvent

pytestmark = pytest.mark.integration


async def create_event(
    database: Database, payment_body: dict[str, JsonValue]
) -> PaymentEvent:
    async with database.sessions() as session:
        await create_payment(
            session, PaymentCreate.model_validate(payment_body), "order-42"
        )
        event = (await session.scalars(select(OutboxEvent))).one()
        return PaymentEvent.model_validate(event.payload)


@pytest.mark.parametrize("payment_status", ["succeeded", "failed"])
async def test_terminal_results_are_not_processed_again(
    database: Database, payment_body: dict[str, JsonValue], payment_status: str
) -> None:
    event = await create_event(database, payment_body)
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(204)

    gateway = AsyncMock()
    gateway.charge.return_value = payment_status
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive)
    ) as client:
        processor = PaymentProcessor(database.sessions, client, gateway)
        await processor.process(event)
        await processor.process(event)

    gateway.charge.assert_awaited_once()
    assert len(requests) == 1
    assert requests[0].headers["Idempotency-Key"] == str(event.event_id)
    async with database.sessions() as session:
        payment = await session.get(Payment, event.payment_id)
        assert payment is not None
        assert payment.status == payment_status
        assert payment.processed_at is not None
        assert payment.webhook_delivered_at is not None
        assert payment.webhook_attempts == 1


async def test_webhook_retry_keeps_payment_result(
    database: Database, payment_body: dict[str, JsonValue]
) -> None:
    event = await create_event(database, payment_body)
    requests: list[httpx.Request] = []

    def receive(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(503 if len(requests) == 1 else 200)

    gateway = AsyncMock()
    gateway.charge.return_value = "succeeded"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(receive)
    ) as client:
        processor = PaymentProcessor(database.sessions, client, gateway)
        with pytest.raises(WebhookDeliveryError):
            await processor.process(event)
        async with database.sessions() as session:
            payment = await session.get(Payment, event.payment_id)
            assert payment is not None
            assert payment.status == "succeeded"
            assert payment.webhook_attempts == 1
            assert payment.webhook_last_error == "Webhook returned HTTP 503"
        await processor.process(event)
    gateway.charge.assert_awaited_once()
    assert requests[0].content == requests[1].content
    async with database.sessions() as session:
        payment = await session.get(Payment, event.payment_id)
        assert payment is not None
        assert payment.webhook_attempts == 2
        assert payment.webhook_last_error is None


async def test_four_failed_webhooks_exhaust_attempts(
    database: Database, payment_body: dict[str, JsonValue]
) -> None:
    event = await create_event(database, payment_body)
    gateway = AsyncMock()
    gateway.charge.return_value = "succeeded"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(500))
    ) as client:
        processor = PaymentProcessor(database.sessions, client, gateway)
        for _ in range(4):
            with pytest.raises(WebhookDeliveryError):
                await processor.process(event)
        with pytest.raises(WebhookAttemptsExhausted):
            await processor.process(event)
    gateway.charge.assert_awaited_once()
    async with database.sessions() as session:
        payment = await session.get(Payment, event.payment_id)
        assert payment is not None
        assert payment.status == "succeeded"
        assert payment.webhook_attempts == 4
        assert payment.webhook_delivered_at is None


async def test_redirects_are_failed_deliveries(
    database: Database, payment_body: dict[str, JsonValue]
) -> None:
    event = await create_event(database, payment_body)
    gateway = AsyncMock()
    gateway.charge.return_value = "failed"
    async with httpx.AsyncClient(
        transport=httpx.MockTransport(lambda _: httpx.Response(302))
    ) as client:
        with pytest.raises(WebhookDeliveryError):
            await PaymentProcessor(database.sessions, client, gateway).process(
                event
            )
