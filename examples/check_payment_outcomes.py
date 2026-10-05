"""Create payments through the API and print each final gateway result."""

import argparse
import time
from collections import Counter
from uuid import UUID, uuid4

import httpx

from src.config import get_settings
from src.schemas import PaymentAccepted, PaymentDetails, PaymentStatus

REQUEST_TIMEOUT_SECONDS = 10


def wait_for_result(client: httpx.Client, payment_id: UUID) -> PaymentDetails:
    deadline = time.monotonic() + 60
    while time.monotonic() < deadline:
        response = client.get(
            f"/api/v1/payments/{payment_id}", timeout=REQUEST_TIMEOUT_SECONDS
        )
        response.raise_for_status()
        payment = PaymentDetails.model_validate(response.json(), by_name=True)
        if payment.status != "pending":
            return payment
        time.sleep(1)
    raise TimeoutError(
        f"Payment {payment_id} is still pending; check the consumer logs."
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--count", type=int, default=30, help="Number of payments to create"
    )
    parser.add_argument("--api-url", default="http://localhost:8000")
    parser.add_argument(
        "--webhook-url", required=True, help="URL reachable from the consumer"
    )
    args = parser.parse_args()
    if args.count < 1:
        parser.error("--count must be at least 1")

    settings = get_settings()
    outcomes: Counter[PaymentStatus] = Counter()
    with httpx.Client(
        base_url=args.api_url,
        headers={"X-API-Key": settings.api_key.get_secret_value()},
        timeout=REQUEST_TIMEOUT_SECONDS,
    ) as client:
        for number in range(1, args.count + 1):
            response = client.post(
                "/api/v1/payments",
                timeout=REQUEST_TIMEOUT_SECONDS,
                headers={"Idempotency-Key": f"outcome-example-{uuid4()}"},
                json={
                    "amount": "1.00",
                    "currency": "USD",
                    "description": "Payment outcome check",
                    "metadata": {"example": "outcome-check"},
                    "webhook_url": args.webhook_url,
                },
            )
            response.raise_for_status()
            accepted = PaymentAccepted.model_validate(
                response.json(), by_name=True
            )
            payment = wait_for_result(client, accepted.payment_id)
            outcomes[payment.status] += 1
            print(
                f"{number}/{args.count}: {payment.payment_id} {payment.status}",
                flush=True,
            )

    print(f"Succeeded: {outcomes['succeeded']}")
    print(f"Failed: {outcomes['failed']}")


if __name__ == "__main__":
    main()
