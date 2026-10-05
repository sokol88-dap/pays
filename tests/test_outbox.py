from unittest.mock import AsyncMock

import pytest
from pydantic import JsonValue
from sqlalchemy import select

from src.config import Settings
from src.database import Database
from src.models import OutboxEvent, utc_now
from src.outbox import OutboxPublisher
from src.payments import create_payment
from src.schemas import PaymentCreate

pytestmark = pytest.mark.integration


async def test_publish_failure_remains_in_outbox(
    database: Database, settings: Settings, payment_body: dict[str, JsonValue]
) -> None:
    async with database.sessions() as session:
        await create_payment(
            session, PaymentCreate.model_validate(payment_body), "order-42"
        )
    broker = AsyncMock()
    broker.publish.side_effect = ConnectionError
    publisher = OutboxPublisher(database.sessions, broker, settings)
    await publisher.publish_batch()
    async with database.sessions() as session, session.begin():
        event = (await session.scalars(select(OutboxEvent))).one()
        assert event.published_at is None
        assert event.publish_attempts == 1
        assert event.last_error == "ConnectionError"
        assert event.next_attempt_at > event.created_at
        event.next_attempt_at = utc_now()

    broker.publish.side_effect = None
    await publisher.publish_batch()
    async with database.sessions() as session:
        event = (await session.scalars(select(OutboxEvent))).one()
        assert event.published_at is not None
        assert event.publish_attempts == 2
        assert event.last_error is None
    assert broker.publish.call_args.kwargs["persist"] is True
    assert broker.publish.call_args.kwargs["mandatory"] is True


async def test_locked_event_is_skipped_by_another_publisher(
    database: Database, settings: Settings, payment_body: dict[str, JsonValue]
) -> None:
    async with database.sessions() as session:
        await create_payment(
            session, PaymentCreate.model_validate(payment_body), "order-42"
        )
    broker = AsyncMock()
    async with database.sessions() as session, session.begin():
        await session.scalars(select(OutboxEvent).with_for_update())
        assert (
            await OutboxPublisher(
                database.sessions, broker, settings
            ).publish_batch()
            == 0
        )
    broker.publish.assert_not_awaited()
