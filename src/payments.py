from uuid import UUID, uuid4

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.ext.asyncio import AsyncSession

from src.models import OutboxEvent, Payment, utc_now
from src.schemas import PaymentCreate, PaymentEvent


class IdempotencyConflict(Exception):
    pass


async def create_payment(
    session: AsyncSession, request: PaymentCreate, idempotency_key: str
) -> Payment:
    """Write the payment and its event in the same database transaction."""
    payment_id = uuid4()
    request_hash = request.request_hash()
    statement = (
        insert(Payment)
        .values(
            id=payment_id,
            amount=request.amount,
            currency=request.currency,
            description=request.description,
            metadata_json=request.metadata,
            webhook_url=str(request.webhook_url),
            idempotency_key=idempotency_key,
            request_hash=request_hash,
            created_at=utc_now(),
        )
        .on_conflict_do_nothing(index_elements=[Payment.idempotency_key])
        .returning(Payment)
    )

    async with session.begin():
        payment = (await session.execute(statement)).scalar_one_or_none()
        if payment is None:
            # PostgreSQL waits concurrent insert before resolving conflict.
            payment = (
                await session.execute(
                    select(Payment).where(
                        Payment.idempotency_key == idempotency_key
                    )
                )
            ).scalar_one()
            if payment.request_hash != request_hash:
                raise IdempotencyConflict
            return payment

        event = PaymentEvent(event_id=uuid4(), payment_id=payment_id)
        session.add(
            OutboxEvent(
                id=event.event_id,
                payment_id=payment_id,
                payload=event.model_dump(mode="json"),
            )
        )
        return payment


async def get_payment(
    session: AsyncSession, payment_id: UUID
) -> Payment | None:
    return await session.get(Payment, payment_id)
