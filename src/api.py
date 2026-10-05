from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from secrets import compare_digest
from typing import Annotated
from uuid import UUID

from fastapi import Depends, FastAPI, Header, HTTPException, Request, status
from fastapi.security import APIKeyHeader
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from src.config import Settings, get_settings
from src.database import Database
from src.payments import IdempotencyConflict, create_payment, get_payment
from src.schemas import PaymentAccepted, PaymentCreate, PaymentDetails

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


async def require_api_key(
    request: Request, api_key: Annotated[str | None, Depends(api_key_header)]
) -> None:
    expected = request.app.state.settings.api_key.get_secret_value()
    if api_key is None or not compare_digest(
        api_key.encode(), expected.encode()
    ):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid API key"
        )


async def get_session(request: Request) -> AsyncGenerator[AsyncSession, None]:
    async with request.app.state.database.sessions() as session:
        yield session


Session = Annotated[AsyncSession, Depends(get_session)]
IdempotencyKey = Annotated[
    str, Header(alias="Idempotency-Key", min_length=1, max_length=255)
]


def create_app(
    settings: Settings | None = None, database: Database | None = None
) -> FastAPI:
    settings = settings or get_settings()
    database = database or Database(settings.database_url)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None, None]:
        yield
        await database.close()

    app = FastAPI(
        title="Payment processing service",
        version="0.1.0",
        lifespan=lifespan,
        dependencies=[Depends(require_api_key)],
        docs_url="/docs",
        redoc_url=None,
        openapi_url="/openapi.json",
    )
    app.state.settings = settings
    app.state.database = database

    @app.post(
        "/api/v1/payments",
        response_model=PaymentAccepted,
        status_code=status.HTTP_202_ACCEPTED,
    )
    async def post_payment(
        body: PaymentCreate, idempotency_key: IdempotencyKey, session: Session
    ) -> PaymentAccepted:
        if not idempotency_key.strip():
            raise HTTPException(
                status_code=422, detail="Idempotency-Key must not be blank"
            )
        try:
            payment = await create_payment(session, body, idempotency_key)
        except IdempotencyConflict as error:
            raise HTTPException(
                status_code=409,
                detail="Idempotency-Key was already used with different data",
            ) from error
        return PaymentAccepted.model_validate(payment)

    @app.get("/api/v1/payments/{payment_id}", response_model=PaymentDetails)
    async def read_payment(
        payment_id: UUID, session: Session
    ) -> PaymentDetails:
        payment = await get_payment(session, payment_id)
        if payment is None:
            raise HTTPException(status_code=404, detail="Payment not found")
        return PaymentDetails.model_validate(payment)

    @app.get("/health", include_in_schema=False)
    async def health(session: Session) -> dict[str, str]:
        await session.execute(text("SELECT 1"))
        return {"status": "ok"}

    return app
