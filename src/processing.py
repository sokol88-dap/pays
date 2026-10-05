import asyncio
import random

import httpx
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from src.messaging import MAX_ATTEMPTS
from src.models import Payment, utc_now
from src.schemas import PaymentEvent, PaymentStatus, PaymentWebhook


class WebhookDeliveryError(Exception):
    pass


class WebhookAttemptsExhausted(Exception):
    pass


class SimulatedGateway:
    async def charge(self) -> PaymentStatus:
        await asyncio.sleep(random.uniform(2, 5))
        return "succeeded" if random.random() < 0.9 else "failed"


class PaymentProcessor:
    def __init__(
        self,
        sessions: async_sessionmaker[AsyncSession],
        http_client: httpx.AsyncClient,
        gateway: SimulatedGateway | None = None,
    ) -> None:
        self.sessions = sessions
        self.http_client = http_client
        self.gateway = gateway or SimulatedGateway()

    async def process(self, event: PaymentEvent) -> None:
        await self.finish_payment(event)
        await self.send_webhook(event)

    async def finish_payment(self, event: PaymentEvent) -> None:
        async with self.sessions() as session, session.begin():
            payment = await self.lock_payment(session, event)
            if payment.status == "pending":
                # The row lock prevents duplicate processing.
                payment.status = await self.gateway.charge()
                payment.processed_at = utc_now()

    async def send_webhook(self, event: PaymentEvent) -> None:
        delivery_error = None
        async with self.sessions() as session, session.begin():
            payment = await self.lock_payment(session, event)
            if payment.webhook_delivered_at is not None:
                return
            if payment.webhook_attempts >= MAX_ATTEMPTS:
                raise WebhookAttemptsExhausted(
                    "Webhook delivery attempts exhausted"
                )
            if payment.processed_at is None:
                raise RuntimeError("Payment has not been processed")

            notification = PaymentWebhook(
                event_id=event.event_id,
                payment_id=payment.id,
                status=payment.status,
                amount=payment.amount,
                currency=payment.currency,
                description=payment.description,
                metadata=payment.metadata_json,
                created_at=payment.created_at,
                processed_at=payment.processed_at,
            )
            payment.webhook_attempts += 1
            try:
                response = await self.http_client.post(
                    payment.webhook_url,
                    timeout=self.http_client.timeout,
                    json=notification.model_dump(mode="json"),
                    headers={"Idempotency-Key": str(event.event_id)},
                )
                if not 200 <= response.status_code < 300:
                    raise WebhookDeliveryError(
                        f"Webhook returned HTTP {response.status_code}"
                    )
            except (httpx.HTTPError, WebhookDeliveryError) as error:
                # Commit the attempt before raising so retries do not reset it.
                payment.webhook_last_error = (
                    str(error)
                    if isinstance(error, WebhookDeliveryError)
                    else type(error).__name__
                )
                delivery_error = WebhookDeliveryError(
                    payment.webhook_last_error
                )
            else:
                payment.webhook_delivered_at = utc_now()
                payment.webhook_last_error = None

        if delivery_error is not None:
            raise delivery_error

    @staticmethod
    async def lock_payment(
        session: AsyncSession, event: PaymentEvent
    ) -> Payment:
        payment = await session.scalar(
            select(Payment)
            .where(Payment.id == event.payment_id)
            .with_for_update()
        )
        if payment is None:
            raise LookupError(f"Payment {event.payment_id} does not exist")
        return payment
