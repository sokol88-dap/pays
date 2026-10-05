"""Local demo receiver.

Run: uvicorn examples.webhook_receiver:app --port 9000.
"""

import logging

from fastapi import FastAPI

from src.schemas import PaymentWebhook

app = FastAPI()
logger = logging.getLogger("uvicorn.error")


@app.post("/webhooks/payments")
async def receive_webhook(notification: PaymentWebhook) -> dict[str, str]:
    logger.info("Payment webhook: %s", notification.model_dump_json())
    return {"status": "received"}


@app.post("/webhooks/payments/fail", status_code=503)
async def reject_webhook(notification: PaymentWebhook) -> dict[str, str]:
    logger.info("Simulated webhook failure: %s", notification.model_dump_json())
    return {"status": "unavailable"}
