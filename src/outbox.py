import asyncio
import logging
from datetime import timedelta
from typing import cast

from faststream.rabbit import RabbitBroker
from faststream.types import SendableMessage
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.config import Settings
from src.messaging import PAYMENTS_EXCHANGE, PAYMENTS_QUEUE
from src.models import OutboxEvent, utc_now

logger = logging.getLogger(__name__)


class OutboxPublisher:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        broker: RabbitBroker,
        settings: Settings,
    ) -> None:
        self.sessions = sessions
        self.broker = broker
        self.settings = settings

    async def publish_batch(self) -> int:
        async with self.sessions() as session, session.begin():
            events = (
                await session.scalars(
                    select(OutboxEvent)
                    .where(
                        OutboxEvent.published_at.is_(None),
                        OutboxEvent.next_attempt_at <= utc_now(),
                    )
                    .order_by(OutboxEvent.created_at)
                    .limit(self.settings.outbox_batch_size)
                    .with_for_update(skip_locked=True)
                )
            ).all()
            for event in events:
                event.publish_attempts += 1
                try:
                    await self.broker.publish(
                        cast(SendableMessage, event.payload),
                        queue=PAYMENTS_QUEUE,
                        exchange=PAYMENTS_EXCHANGE,
                        message_id=str(event.id),
                        headers={"x-attempt": 1},
                        persist=True,
                        mandatory=True,
                        timeout=self.settings.publish_timeout_seconds,
                    )
                except Exception as error:
                    event.last_error = type(error).__name__
                    delay = min(2 ** min(event.publish_attempts - 1, 6), 60)
                    event.next_attempt_at = utc_now() + timedelta(seconds=delay)
                    logger.warning(
                        "Outbox publish failed for event %s",
                        event.id,
                        exc_info=True,
                    )
                else:
                    event.published_at = utc_now()
                    event.last_error = None
            return len(events)

    async def run(self) -> None:
        """
        Unpublished events remain durable; broker outages do not exhaust them.
        """
        while True:
            try:
                await self.publish_batch()
            except Exception:
                logger.exception("Outbox batch failed. It will be retried")
            await asyncio.sleep(self.settings.outbox_poll_seconds)
