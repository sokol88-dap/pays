import hashlib
import json
from datetime import datetime
from decimal import Decimal
from typing import Annotated, Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    HttpUrl,
    JsonValue,
    field_validator,
)

Currency = Literal["RUB", "USD", "EUR"]
PaymentStatus = Literal["pending", "succeeded", "failed"]
Amount = Annotated[Decimal, Field(gt=0, max_digits=18, decimal_places=2)]


class PaymentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    amount: Amount
    currency: Currency
    description: str = Field(max_length=2000)
    metadata: JsonValue = Field(default_factory=dict)
    webhook_url: HttpUrl

    @field_validator("metadata")
    @classmethod
    def require_finite_json(cls, value: JsonValue) -> JsonValue:
        json.dumps(value, allow_nan=False)
        return value

    @field_validator("webhook_url")
    @classmethod
    def disallow_url_credentials(cls, url: HttpUrl) -> HttpUrl:
        if url.username or url.password:
            raise ValueError("webhook_url must not contain credentials")
        return url

    def request_hash(self) -> str:
        """
        Compare normalized requests, including amounts such as 10 and 10.00.
        """
        body = self.model_dump(mode="json")
        body["amount"] = format(self.amount.quantize(Decimal("0.01")), ".2f")
        canonical_json = json.dumps(
            body, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        return hashlib.sha256(canonical_json.encode()).hexdigest()


class PaymentAccepted(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    payment_id: UUID = Field(validation_alias="id")
    status: PaymentStatus
    created_at: datetime


class PaymentDetails(PaymentAccepted):
    amount: Decimal
    currency: Currency
    description: str
    metadata: JsonValue = Field(validation_alias="metadata_json")
    idempotency_key: str
    webhook_url: str
    processed_at: datetime | None
    webhook_delivered_at: datetime | None
    webhook_attempts: int
    webhook_last_error: str | None


class PaymentEvent(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: UUID
    payment_id: UUID


class PaymentWebhook(BaseModel):
    event_id: UUID
    payment_id: UUID
    status: PaymentStatus
    amount: Decimal
    currency: Currency
    description: str
    metadata: JsonValue
    created_at: datetime
    processed_at: datetime
