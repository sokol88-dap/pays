import math

import httpx
import pytest
from pydantic import JsonValue, ValidationError

from src.api import create_app
from src.config import Settings
from src.schemas import PaymentCreate


@pytest.mark.parametrize(
    "amount", ["0", "-1", "1.001", "NaN", "Infinity", "10000000000000000"]
)
def test_invalid_amounts_are_rejected(
    payment_body: dict[str, JsonValue], amount: str
) -> None:
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate({**payment_body, "amount": amount})


def test_request_hash_normalizes_amount_and_metadata_order(
    payment_body: dict[str, JsonValue],
) -> None:
    first = PaymentCreate.model_validate(
        {**payment_body, "amount": "10", "metadata": {"a": 1, "b": 2}}
    )
    second = PaymentCreate.model_validate(
        {**payment_body, "amount": "10.00", "metadata": {"b": 2, "a": 1}}
    )
    assert first.request_hash() == second.request_hash()


@pytest.mark.parametrize(
    "changes",
    [
        {"currency": "GBP"},
        {"webhook_url": "ftp://merchant.example/payments"},
        {"webhook_url": "https://user:password@merchant.example/payments"},
        {"metadata": {"value": math.inf}},
        {"unexpected": True},
    ],
)
def test_invalid_request_fields(
    payment_body: dict[str, JsonValue], changes: dict[str, JsonValue]
) -> None:
    with pytest.raises(ValidationError):
        PaymentCreate.model_validate({**payment_body, **changes})


@pytest.mark.parametrize(
    "metadata", [None, [], {"nested": [1, True, None]}, "value", 42]
)
def test_metadata_accepts_json_values(
    payment_body: dict[str, JsonValue], metadata: JsonValue
) -> None:
    request = PaymentCreate.model_validate(
        {**payment_body, "metadata": metadata}
    )
    assert request.metadata == metadata


@pytest.mark.parametrize("path", ["/health", "/api/v1/payments/invalid"])
@pytest.mark.parametrize("headers", [{}, {"X-API-Key": "wrong"}])
async def test_payment_and_health_endpoints_require_authentication(
    settings: Settings, path: str, headers: dict[str, str]
) -> None:
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.get(path, headers=headers)
    await app.state.database.close()
    assert response.status_code == 401


async def test_post_requires_authentication(
    settings: Settings, payment_body: dict[str, JsonValue]
) -> None:
    app = create_app(settings)
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app), base_url="http://test"
    ) as client:
        response = await client.post("/api/v1/payments", json=payment_body)
    await app.state.database.close()
    assert response.status_code == 401
