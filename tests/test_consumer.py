from unittest.mock import AsyncMock
from uuid import uuid4

import pytest

from src.config import Settings
from src.consumer import PaymentConsumer
from src.messaging import DEAD_LETTER_QUEUE, RETRY_QUEUES


@pytest.fixture
def event_body() -> dict[str, str]:
    return {"event_id": str(uuid4()), "payment_id": str(uuid4())}


@pytest.mark.parametrize("attempt", [1, 2, 3])
async def test_failed_events_go_to_delayed_retry(
    settings: Settings, event_body: dict[str, str], attempt: int
) -> None:
    processor = AsyncMock()
    processor.process.side_effect = RuntimeError("temporary failure")
    broker = AsyncMock()
    message = AsyncMock()
    message.headers = {"x-attempt": attempt}
    await PaymentConsumer(processor, broker, settings).handle(
        event_body, message
    )
    publish = broker.publish.call_args
    assert publish.kwargs["queue"] == RETRY_QUEUES[attempt - 1]
    assert publish.kwargs["headers"] == {"x-attempt": attempt + 1}
    assert publish.kwargs["persist"] is True
    message.ack.assert_awaited_once()
    message.nack.assert_not_awaited()


async def test_fourth_failure_goes_to_dlq(
    settings: Settings, event_body: dict[str, str]
) -> None:
    processor = AsyncMock()
    processor.process.side_effect = RuntimeError("permanent failure")
    broker = AsyncMock()
    message = AsyncMock()
    message.headers = {"x-attempt": 4}
    await PaymentConsumer(processor, broker, settings).handle(
        event_body, message
    )
    assert broker.publish.call_args.kwargs["queue"] == DEAD_LETTER_QUEUE
    assert broker.publish.call_args.args[0]["event"] == event_body
    assert broker.publish.call_args.args[0]["attempts"] == 4
    message.ack.assert_awaited_once()


async def test_unconfirmed_retry_keeps_original_message(
    settings: Settings,
    event_body: dict[str, str],
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr("src.consumer.asyncio.sleep", AsyncMock())
    processor = AsyncMock()
    processor.process.side_effect = RuntimeError("temporary failure")
    broker = AsyncMock()
    broker.publish.side_effect = ConnectionError
    message = AsyncMock()
    message.headers = {"x-attempt": 1}
    await PaymentConsumer(processor, broker, settings).handle(
        event_body, message
    )
    message.ack.assert_not_awaited()
    message.nack.assert_awaited_once_with(requeue=True)


async def test_success_is_acknowledged(
    settings: Settings, event_body: dict[str, str]
) -> None:
    processor = AsyncMock()
    broker = AsyncMock()
    message = AsyncMock()
    message.headers = {}
    await PaymentConsumer(processor, broker, settings).handle(
        event_body, message
    )
    message.ack.assert_awaited_once()
    broker.publish.assert_not_awaited()
