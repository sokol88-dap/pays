import asyncio
import logging
from contextlib import suppress
from typing import Any

import httpx
from faststream import AckPolicy, FastStream
from faststream.rabbit.annotations import RabbitMessage

from src.config import Settings, get_settings
from src.consumer import PaymentConsumer
from src.database import Database
from src.messaging import (
    PAYMENTS_EXCHANGE,
    PAYMENTS_QUEUE,
    create_broker,
    declare_topology,
)
from src.outbox import OutboxPublisher
from src.processing import PaymentProcessor


def create_worker(settings: Settings) -> FastStream:
    database = Database(settings.database_url)
    broker = create_broker(settings)
    http_client = httpx.AsyncClient(
        timeout=settings.webhook_timeout_seconds,
        follow_redirects=False,
        trust_env=False,
    )
    processor = PaymentProcessor(database.sessions, http_client)
    consumer = PaymentConsumer(processor, broker, settings)
    outbox = OutboxPublisher(database.sessions, broker, settings)
    app = FastStream(broker)
    relay_task: asyncio.Task[None] | None = None

    @broker.subscriber(
        PAYMENTS_QUEUE, PAYMENTS_EXCHANGE, ack_policy=AckPolicy.MANUAL
    )
    async def handle_payment(body: Any, message: RabbitMessage) -> None:
        await consumer.handle(body, message)

    @app.on_startup
    async def prepare_queues() -> None:
        await broker.connect()
        await declare_topology(broker)

    @app.after_startup
    async def start_outbox() -> None:
        nonlocal relay_task
        relay_task = asyncio.create_task(outbox.run(), name="outbox-publisher")

    @app.on_shutdown
    async def stop_outbox() -> None:
        if relay_task is not None:
            relay_task.cancel()
            with suppress(asyncio.CancelledError):
                await relay_task

    @app.after_shutdown
    async def close_resources() -> None:
        await http_client.aclose()
        await database.close()

    return app


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    asyncio.run(create_worker(get_settings()).run())


if __name__ == "__main__":
    main()
