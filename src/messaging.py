from faststream.rabbit import (
    Channel,
    QueueType,
    RabbitBroker,
    RabbitExchange,
    RabbitQueue,
)
from faststream.rabbit.schemas.queue import QuorumQueueArgs

from src.config import Settings

RETRY_DELAYS_SECONDS = (1, 2, 4)
MAX_ATTEMPTS = 1 + len(RETRY_DELAYS_SECONDS)

PAYMENTS_EXCHANGE = RabbitExchange("payments", durable=True)
RETRY_EXCHANGE = RabbitExchange("payments.retry", durable=True)
DEAD_LETTER_EXCHANGE = RabbitExchange("payments.dlx", durable=True)


def reliable_dead_lettering(exchange: str, routing_key: str) -> QuorumQueueArgs:
    return {
        "x-dead-letter-exchange": exchange,
        "x-dead-letter-routing-key": routing_key,
        "x-dead-letter-strategy": "at-least-once",
        "x-overflow": "reject-publish",
    }


PAYMENTS_QUEUE = RabbitQueue(
    "payments.new",
    queue_type=QueueType.QUORUM,
    durable=True,
    routing_key="payments.new",
    arguments={
        **reliable_dead_lettering("payments.dlx", "payments.dlq"),
        "x-single-active-consumer": True,
        "x-delivery-limit": 1000,
    },
)
DEAD_LETTER_QUEUE = RabbitQueue(
    "payments.dlq",
    queue_type=QueueType.QUORUM,
    durable=True,
    routing_key="payments.dlq",
)
RETRY_QUEUES = tuple(
    RabbitQueue(
        f"payments.retry.{delay}s",
        queue_type=QueueType.QUORUM,
        durable=True,
        routing_key=f"payments.retry.{delay}s",
        arguments={
            **reliable_dead_lettering("payments", "payments.new"),
            "x-message-ttl": delay * 1000,
        },
    )
    for delay in RETRY_DELAYS_SECONDS
)


def create_broker(settings: Settings) -> RabbitBroker:
    return RabbitBroker(
        settings.rabbitmq_url,
        default_channel=Channel(
            prefetch_count=1, publisher_confirms=True, on_return_raises=True
        ),
        graceful_timeout=30,
    )


async def declare_topology(broker: RabbitBroker) -> None:
    """Declare every destination before the outbox is allowed to publish."""
    for exchange in (PAYMENTS_EXCHANGE, RETRY_EXCHANGE, DEAD_LETTER_EXCHANGE):
        await broker.declare_exchange(exchange)
    for queue, exchange in (
        (PAYMENTS_QUEUE, PAYMENTS_EXCHANGE),
        (DEAD_LETTER_QUEUE, DEAD_LETTER_EXCHANGE),
        *((queue, RETRY_EXCHANGE) for queue in RETRY_QUEUES),
    ):
        declared_queue = await broker.declare_queue(queue)
        await declared_queue.bind(exchange.name, routing_key=queue.routing_key)
