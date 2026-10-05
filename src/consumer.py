import asyncio
import logging
from typing import Any

from faststream.rabbit import RabbitBroker
from faststream.rabbit.message import RabbitMessage

from src.config import Settings
from src.messaging import (
    DEAD_LETTER_EXCHANGE,
    DEAD_LETTER_QUEUE,
    MAX_ATTEMPTS,
    RETRY_EXCHANGE,
    RETRY_QUEUES,
)
from src.models import utc_now
from src.processing import PaymentProcessor, WebhookAttemptsExhausted
from src.schemas import PaymentEvent

logger = logging.getLogger(__name__)


class PaymentConsumer:
    """
    One handler processes the payment, then sends or retries its notification.
    If the notification fails, the message is sent to a retry queue or
    dead-letter queue.
    """

    def __init__(
        self,
        processor: PaymentProcessor,
        broker: RabbitBroker,
        settings: Settings,
    ) -> None:
        self.processor = processor
        self.broker = broker
        self.settings = settings

    async def handle(self, body: Any, message: RabbitMessage) -> None:
        try:
            attempt = int(message.headers.get("x-attempt", 1))
            if not 1 <= attempt <= MAX_ATTEMPTS:
                raise ValueError
        except (TypeError, ValueError):
            attempt = MAX_ATTEMPTS

        try:
            event = PaymentEvent.model_validate(body)
            await self.processor.process(event)
        except Exception as error:
            logger.warning(
                "Payment event failed on attempt %s", attempt, exc_info=True
            )
            try:
                if attempt >= MAX_ATTEMPTS or isinstance(
                    error, WebhookAttemptsExhausted
                ):
                    await self.broker.publish(
                        {
                            "event": body,
                            "attempts": attempt,
                            "error": type(error).__name__,
                            "failed_at": utc_now().isoformat(),
                        },
                        queue=DEAD_LETTER_QUEUE,
                        exchange=DEAD_LETTER_EXCHANGE,
                        persist=True,
                        mandatory=True,
                        timeout=self.settings.publish_timeout_seconds,
                        message_id=message.message_id,
                    )
                else:
                    await self.broker.publish(
                        body,
                        queue=RETRY_QUEUES[attempt - 1],
                        exchange=RETRY_EXCHANGE,
                        headers={"x-attempt": attempt + 1},
                        persist=True,
                        mandatory=True,
                        timeout=self.settings.publish_timeout_seconds,
                        message_id=message.message_id,
                    )
            except Exception:
                logger.exception(
                    "Could not transfer event. Retry original delivery"
                )
                await asyncio.sleep(1)
                await message.nack(requeue=True)
                return

        await message.ack()
