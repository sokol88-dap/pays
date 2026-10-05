import asyncio
import json
import os
from collections.abc import AsyncGenerator
from time import monotonic
from unittest.mock import AsyncMock
from urllib.parse import unquote, urlparse
from uuid import uuid4

import pytest
import pytest_asyncio
from faststream import AckPolicy
from faststream.rabbit import RabbitBroker
from faststream.rabbit.annotations import RabbitMessage
from pydantic import JsonValue

from src.config import Settings
from src.consumer import PaymentConsumer
from src.messaging import (
    DEAD_LETTER_QUEUE,
    PAYMENTS_EXCHANGE,
    PAYMENTS_QUEUE,
    RETRY_QUEUES,
    create_broker,
    declare_topology,
)
from src.schemas import PaymentEvent

pytestmark = pytest.mark.integration


@pytest_asyncio.fixture
async def broker(settings: Settings) -> AsyncGenerator[RabbitBroker, None]:
    url = os.environ.get("TEST_RABBITMQ_URL")
    if not url:
        pytest.skip("Set TEST_RABBITMQ_URL to run RabbitMQ integration tests")
    if not unquote(urlparse(url).path).endswith("_test"):
        pytest.fail(
            "TEST_RABBITMQ_URL must name a dedicated vhost ending in _test"
        )
    broker = create_broker(settings.model_copy(update={"rabbitmq_url": url}))
    await broker.connect()
    await declare_topology(broker)
    for queue in (PAYMENTS_QUEUE, DEAD_LETTER_QUEUE, *RETRY_QUEUES):
        declared = await broker.declare_queue(queue)
        await declared.purge()
    yield broker
    await broker.stop()


async def test_real_broker_retries_then_dead_letters(
    broker: RabbitBroker, settings: Settings
) -> None:
    times: list[float] = []
    final_failure = asyncio.Event()

    async def fail_processing(event: PaymentEvent) -> None:
        times.append(monotonic())
        raise RuntimeError("Permanent processing failure")

    processor = AsyncMock()
    processor.process.side_effect = fail_processing
    consumer = PaymentConsumer(processor, broker, settings)

    @broker.subscriber(
        PAYMENTS_QUEUE, PAYMENTS_EXCHANGE, ack_policy=AckPolicy.MANUAL
    )
    async def handle(
        body: dict[str, JsonValue], message: RabbitMessage
    ) -> None:
        await consumer.handle(body, message)
        if len(times) == 4:
            final_failure.set()

    await broker.start()
    body = PaymentEvent(event_id=uuid4(), payment_id=uuid4()).model_dump(
        mode="json"
    )
    await broker.publish(
        body,
        queue=PAYMENTS_QUEUE,
        exchange=PAYMENTS_EXCHANGE,
        persist=True,
        headers={"x-attempt": 1},
    )
    async with asyncio.timeout(20):
        await final_failure.wait()
        dlq = await broker.declare_queue(DEAD_LETTER_QUEUE)
        message = await dlq.get(timeout=5)
    async with message.process():
        failed_event = json.loads(message.body)
        assert failed_event["event"] == body
        assert failed_event["attempts"] == 4
    assert len(times) == 4
    for first, second, minimum_delay in zip(
        times, times[1:], (1, 2, 4), strict=False
    ):
        assert second - first >= minimum_delay * 0.9
