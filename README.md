# Asynchronous payment processing

A Python microservice that accepts a payment request, processes it in the
background, and sends the result to your webhook URL. You can also retrieve the
payment's current status through the API.

The payment gateway is simulated: processing takes 2–5 seconds, with a 90% chance
of success and a 10% chance of failure. No money is transferred.

- [Start with Docker](#start-with-docker)
- [Create and retrieve a payment](#create-and-retrieve-a-payment)
- [Receive webhook notifications](#receive-webhook-notifications)
- [Check failures and retries](#check-failures-and-retries)
- [Configure the service](#configure-the-service)
- [Local development and checks](#local-development-and-checks)
- [GitHub CI/CD](#github-cicd)
- [How processing works](#how-processing-works)

## Start with Docker

You need Docker with Compose. Run these commands from the project root:

```sh
cp .env.example .env
docker compose up -d --build
```

This starts PostgreSQL, RabbitMQ, the API, and the consumer. Database migrations
run automatically before the API starts. To watch payment processing:

```sh
docker compose logs -f consumer
```

| Service | Local address | Example credentials |
| --- | --- | --- |
| Payment API | `http://localhost:8000` | `X-API-Key: local-development-key` |
| Swagger UI | `http://localhost:8000/docs` | Open directly in your browser |
| RabbitMQ management UI | `http://localhost:15672` | User `pays`, password `pays` |
| RabbitMQ broker | `localhost:5672` | User `pays`, password `pays` |
| PostgreSQL | `localhost:5432` | User `pays`, password `pays`, database `pays` |

These addresses are bound to localhost. The credentials above match
`.env.example`; use your configured values if you change them.

Every curl example below uses a 5-second connection timeout and a 10-second
total request timeout.

Open [Swagger UI](http://localhost:8000/docs) to browse and test the API.
Click **Authorize**, enter your `API_KEY` value (just the key), and click
**Authorize** again. Select an endpoint and use **Try it out** to send a request.
The documentation and its `/openapi.json` schema are accessible without a key;
payment requests and `/health` require `X-API-Key`.

Check that the API can reach PostgreSQL:

```sh
curl --connect-timeout 5 --max-time 10 \
  http://localhost:8000/health \
  -H 'X-API-Key: local-development-key'
```

```json
{"status": "ok"}
```

To stop the containers while keeping stored payments and queued messages:

```sh
docker compose down
```

## Create and retrieve a payment

Payment endpoints and `/health` require `X-API-Key`. The examples below use the
key from `.env.example`.

### Prepare a webhook receiver

Use your own HTTP endpoint, or run the included demo receiver in another terminal.
The demo prints each notification and responds with `200 OK`.

If you have not created a Python environment yet:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements.txt
```

Then start the receiver:

```sh
.venv/bin/uvicorn examples.webhook_receiver:app --host 0.0.0.0 --port 9000
```

On Docker Desktop, the consumer reaches this host process through
`host.docker.internal`. On Linux, provide a receiver address reachable from the
containers, or configure a `host-gateway` mapping. When the consumer runs directly
on your host, use `http://localhost:9000/webhooks/payments`.

### Create a payment

Send `POST /api/v1/payments` with an `Idempotency-Key` identifying this request:

```sh
curl --connect-timeout 5 --max-time 10 -i \
  http://localhost:8000/api/v1/payments \
  -H 'X-API-Key: local-development-key' \
  -H 'Idempotency-Key: order-42' \
  -H 'Content-Type: application/json' \
  -d '{
    "amount": "125.50",
    "currency": "RUB",
    "description": "Order 42",
    "metadata": {"order_id": 42},
    "webhook_url": "http://host.docker.internal:9000/webhooks/payments"
  }'
```

Example response — **202 Accepted**:

```json
{
  "payment_id": "7a0d4af6-6126-45cb-a73c-ec73ec66c13d",
  "status": "pending",
  "created_at": "2026-10-05T12:00:00Z"
}
```

`202` means the payment was accepted for background processing. It does not mean
the payment succeeded.

| Body field | Required | Accepted values |
| --- | --- | --- |
| `amount` | Yes | A positive decimal with up to 16 digits before the decimal point and 2 after it. Use a string such as `"125.50"`. |
| `currency` | Yes | `RUB`, `USD`, or `EUR`. |
| `description` | Yes | A string of at most 2,000 characters. |
| `metadata` | No | Any JSON value with finite numbers. Defaults to `{}`. |
| `webhook_url` | Yes | An HTTP or HTTPS URL without embedded credentials. |

Unknown body fields are rejected. `Idempotency-Key` must be a nonblank string of
at most 255 characters.

**Retrying a request:** send the same key and the same data to retrieve the
existing payment with `202`. Its ID and creation time stay the same; the response
shows its current status. Reusing the key with different data returns `409`.
Concurrent copies of the same request create one payment and one event.

### Retrieve a payment

Send `GET /api/v1/payments/{payment_id}`, replacing the example ID with the ID from
your creation response:

```sh
curl --connect-timeout 5 --max-time 10 \
  http://localhost:8000/api/v1/payments/7a0d4af6-6126-45cb-a73c-ec73ec66c13d \
  -H 'X-API-Key: local-development-key'
```

Example response after processing and webhook delivery — **200 OK**:

```json
{
  "payment_id": "7a0d4af6-6126-45cb-a73c-ec73ec66c13d",
  "status": "succeeded",
  "created_at": "2026-10-05T12:00:00Z",
  "amount": "125.50",
  "currency": "RUB",
  "description": "Order 42",
  "metadata": {"order_id": 42},
  "idempotency_key": "order-42",
  "webhook_url": "http://host.docker.internal:9000/webhooks/payments",
  "processed_at": "2026-10-05T12:00:03Z",
  "webhook_delivered_at": "2026-10-05T12:00:03.100000Z",
  "webhook_attempts": 1,
  "webhook_last_error": null
}
```

Amounts are returned as decimal strings. Timestamps include a timezone; these
examples use UTC (`Z`).

| Status | Meaning |
| --- | --- |
| `pending` | Processing has not finished. `processed_at` is `null`. |
| `succeeded` | The simulated payment succeeded. |
| `failed` | The simulated gateway declined the payment. |

`webhook_delivered_at` is `null` until a notification is delivered.
`webhook_attempts` counts recorded delivery attempts, and `webhook_last_error`
contains the most recent delivery error or `null`. A webhook failure does not
change the payment's final status.

### Error responses

| HTTP status | When it happens |
| --- | --- |
| `401 Unauthorized` | `X-API-Key` is missing or incorrect. |
| `404 Not Found` | The requested payment does not exist. |
| `409 Conflict` | An idempotency key is reused with different request data. |
| `422 Unprocessable Entity` | The request body, path ID, or required idempotency key fails validation. |

Example — **404 Not Found**:

```json
{"detail": "Payment not found"}
```

Field validation errors use a `detail` array describing the invalid fields.
A whitespace-only `Idempotency-Key` returns a `detail` string instead.
To import the API schema into a client, fetch `/openapi.json`.

## Receive webhook notifications

After processing, the consumer sends an HTTP POST to the payment's `webhook_url`.

Example notification:

```json
{
  "event_id": "326a9b08-6b44-440e-a290-1c07a81a8baf",
  "payment_id": "7a0d4af6-6126-45cb-a73c-ec73ec66c13d",
  "status": "succeeded",
  "amount": "125.50",
  "currency": "RUB",
  "description": "Order 42",
  "metadata": {"order_id": 42},
  "created_at": "2026-10-05T12:00:00Z",
  "processed_at": "2026-10-05T12:00:03Z"
}
```

The request also includes `Idempotency-Key: <event_id>`. Store this ID at your
receiver to recognize repeated notifications. The service's API key is not sent
to the receiver.

Any `2xx` response confirms delivery. Timeouts, connection errors, redirects, and
other HTTP statuses count as delivery failures. Redirects are not followed.

After a processing or delivery error, the consumer retries after **1, 2, and 4
seconds**: one initial attempt plus three retries. A gateway result of `failed`
is a final payment result; it is sent to the receiver just like `succeeded`.
Notification retries reuse the saved result and do not simulate another charge.

After the fourth unsuccessful attempt, the event is retained in `payments.dlq`,
the dead-letter queue. A payment with an undelivered webhook keeps its final
status. If an event fails before payment processing finishes, that payment stays
`pending` for investigation. Inspect dead letters in the RabbitMQ management UI;
there is no automatic replay endpoint.

## Check failures and retries

There are three kinds of failure to check:

| Scenario | What you should see |
| --- | --- |
| The simulated gateway declines a payment | Creation returns `202`; retrieving the payment returns `200` with `status: "failed"`. The webhook also contains `"failed"`. |
| The webhook receiver is unavailable | The payment keeps its final status. Delivery is attempted four times, then the event goes to `payments.dlq`. |
| An API request is invalid | The API returns `401`, `404`, `409`, or `422`, with a `detail` explaining the error. |

### Observe the random 10% payment failures

Each new payment has an independent 10% chance of being declined. This does not
guarantee one failure in every ten payments. A small batch can contain no failures,
and reusing an idempotency key returns the same payment rather than a new outcome.

Start the [demo webhook receiver](#prepare-a-webhook-receiver), then run:

```sh
.venv/bin/python -m examples.check_payment_outcomes \
  --count 30 \
  --webhook-url http://host.docker.internal:9000/webhooks/payments
```

The script reads `API_KEY` from your environment or `.env`, creates 30 payments
with unique idempotency keys, waits for each final result, and prints a summary.
Every HTTP request sets a 10-second timeout for connection, read, write, and
connection-pool waits.
Allow a few minutes because the consumer processes payments one at a time.
For a consumer running directly on your host, use a webhook URL starting with
`http://localhost:9000`. Use `--api-url` if your API is at a different address.

Example output; your counts will vary:

```text
1/30: 7a0d4af6-6126-45cb-a73c-ec73ec66c13d succeeded
2/30: 389a351d-c45f-4a76-a788-21ccb319f1ee failed
...
Succeeded: 27
Failed: 3
```

Retrieve a payment whose printed status is `failed`:

```sh
curl --connect-timeout 5 --max-time 10 \
  http://localhost:8000/api/v1/payments/389a351d-c45f-4a76-a788-21ccb319f1ee \
  -H 'X-API-Key: local-development-key'
```

Example response after its notification has been delivered — **200 OK**:

```json
{
  "payment_id": "389a351d-c45f-4a76-a788-21ccb319f1ee",
  "status": "failed",
  "created_at": "2026-10-05T12:00:00Z",
  "amount": "1.00",
  "currency": "USD",
  "description": "Payment outcome check",
  "metadata": {"example": "outcome-check"},
  "idempotency_key": "outcome-example-326a9b08-6b44-440e-a290-1c07a81a8baf",
  "webhook_url": "http://host.docker.internal:9000/webhooks/payments",
  "processed_at": "2026-10-05T12:00:03Z",
  "webhook_delivered_at": "2026-10-05T12:00:03.100000Z",
  "webhook_attempts": 1,
  "webhook_last_error": null
}
```

Use the ID from your own output. The gateway decline is a final result, so the
consumer sends its notification and does not retry payment processing. The script
waits for the payment result; webhook delivery may still be in progress when the
result is printed. Thirty payments will usually include a decline, but it remains
random. For a guaranteed failure case, use the automated checks below.

### Check webhook retries and the dead-letter queue

The demo receiver also has `/webhooks/payments/fail`, which always returns `503`.
If it was already running before this endpoint was added, restart the receiver.
Create one payment pointing to it:

```sh
.venv/bin/python -m examples.check_payment_outcomes \
  --count 1 \
  --webhook-url http://host.docker.internal:9000/webhooks/payments/fail
```

Use the printed payment ID in `GET /api/v1/payments/{payment_id}`. Repeat the GET
until `webhook_attempts` reaches `4`. Retries occur after 1, 2, and 4 seconds;
processing and queue activity can add more time.

The delivery fields should then look like this:

```json
{
  "webhook_delivered_at": null,
  "webhook_attempts": 4,
  "webhook_last_error": "Webhook returned HTTP 503"
}
```

The payment's status stays `succeeded` or `failed`, whichever result the gateway
originally produced. The receiver logs each attempted notification. To see the
consumer's errors:

```sh
docker compose logs -f consumer
```

After the final transfer, inspect `payments.dlq` in the
[RabbitMQ management UI](http://localhost:15672). The failed event is retained
there for investigation.

### Run repeatable automated failure checks

The processing tests supply controlled gateway results and webhook responses,
so failures are covered without relying on random chance. They verify that:

- Both `succeeded` and `failed` results are saved, notified, and never processed again.
- A `503` followed by `200` retries delivery while retaining the original result.
- Four failed notifications exhaust delivery attempts.
- A redirect counts as a failed notification.

First create and migrate a dedicated test database using the
[integration test setup](#integration-tests), then run:

```sh
TEST_DATABASE_URL=postgresql+asyncpg://pays:pays@localhost:5432/pays_test \
  .venv/bin/python -m pytest -q tests/test_processing.py
```

These five tests need PostgreSQL; the webhook responses are simulated, so they
do not need a running receiver or RabbitMQ. Adjust the database password if you
changed it.

### Check API error responses

These commands use the example API key. Replace it if you changed `API_KEY`.

**401 — missing API key:**

```sh
curl --connect-timeout 5 --max-time 10 -i http://localhost:8000/health
```

```json
{"detail": "Invalid API key"}
```

**404 — payment does not exist:**

```sh
curl --connect-timeout 5 --max-time 10 -i \
  http://localhost:8000/api/v1/payments/00000000-0000-0000-0000-000000000000 \
  -H 'X-API-Key: local-development-key'
```

```json
{"detail": "Payment not found"}
```

**422 — unsupported currency:**

```sh
curl --connect-timeout 5 --max-time 10 -i \
  http://localhost:8000/api/v1/payments \
  -H 'X-API-Key: local-development-key' \
  -H 'Idempotency-Key: invalid-currency-example' \
  -H 'Content-Type: application/json' \
  -d '{
    "amount": "1.00",
    "currency": "GBP",
    "description": "Invalid currency check",
    "webhook_url": "http://host.docker.internal:9000/webhooks/payments"
  }'
```

The response's `detail` array identifies `body.currency` and lists the allowed
currencies. The request is rejected before a payment or event is created.

**409 — idempotency key reused with different data:** first send the
[create-payment example](#create-a-payment) with `Idempotency-Key: order-42`.
Repeat that request with the same key and change its amount from `"125.50"` to
`"150.00"`. The response is:

```json
{"detail": "Idempotency-Key was already used with different data"}
```

## Configure the service

Copy `.env.example` to `.env` and edit the values you need. The example includes
every supported application and Compose setting.

### Application settings

The Python application reads `.env` from its working directory. Values supplied
in the process environment take precedence. Unknown `.env` entries are ignored.

| Variable | Default | Purpose and allowed values |
| --- | --- | --- |
| `API_KEY` | No default; required | Key expected in payment and health requests' `X-API-Key` header. Must be a nonempty string. The example file uses `local-development-key`. |
| `DATABASE_URL` | `postgresql+asyncpg://pays:pays@localhost:5432/pays` | PostgreSQL URL for local Python runs, including Alembic. Use the `postgresql+asyncpg` driver. |
| `RABBITMQ_URL` | `amqp://pays:pays@localhost:5672/` | RabbitMQ URL for a local consumer. The trailing `/` selects the default vhost. |
| `WEBHOOK_TIMEOUT_SECONDS` | `10` | HTTP timeout for webhook requests, in seconds. Any number greater than zero. |
| `OUTBOX_POLL_SECONDS` | `1` | Seconds between checks for events waiting to be published. Any number greater than zero. |
| `OUTBOX_BATCH_SIZE` | `50` | Maximum events selected for one publication batch. An integer from `1` to `1000`. |
| `PUBLISH_TIMEOUT_SECONDS` | `10` | Maximum wait for RabbitMQ to confirm a publication, in seconds. Any number greater than zero. |

Docker Compose forwards `API_KEY` and all four timeout, polling, and batch settings
to the application containers. It builds `DATABASE_URL` and `RABBITMQ_URL` using
the internal hostnames `postgres` and `rabbitmq`, so the URLs in `.env` apply only
when running Python on your host. To use external databases or brokers from the
containers, update those connection URLs in `compose.yaml`.

The retry schedule is fixed at 1, 2, and 4 seconds. There is no environment variable
for changing the retry count or these delays.

### Docker credentials

| Variable | Default | Purpose |
| --- | --- | --- |
| `POSTGRES_PASSWORD` | `pays` | Password for PostgreSQL's `pays` user. Compose also uses it in the application's database URL. |
| `RABBITMQ_PASSWORD` | `pays` | Password for RabbitMQ's `pays` user, including the management UI. Compose also uses it in the application's broker URL. |

Compose inserts these passwords directly into connection URLs. Use URL-safe
characters, such as letters, numbers, underscores, and hyphens. For manually
configured connection URLs, URL-encode credentials that contain reserved characters.

These credentials initialize the services' data volumes. Changing a password in
`.env` after a volume has been initialized also requires updating that user's
password in the corresponding service. Recreating a container preserves its volume.

For example, to use a five-second webhook timeout and smaller publication batches:

```dotenv
API_KEY=local-development-key
POSTGRES_PASSWORD=pays
RABBITMQ_PASSWORD=pays
WEBHOOK_TIMEOUT_SECONDS=5
OUTBOX_POLL_SECONDS=1
OUTBOX_BATCH_SIZE=20
PUBLISH_TIMEOUT_SECONDS=10
```

After editing `.env`, apply the values to the application containers:

```sh
docker compose up -d
```

For local Python runs, restart the API and consumer processes.

## Local development and checks

Use Python 3.12 or later. Docker uses Python 3.14. Run commands from the project
root; the application package is `src`.

Install the application and development tools:

```sh
python3 -m venv .venv
.venv/bin/python -m pip install -r requirements-dev.txt
cp .env.example .env
```

If the full Docker service is already running, stop its API and consumer first
to free port `8000` and let the local consumer handle messages:

```sh
docker compose stop api consumer
```

Start PostgreSQL and RabbitMQ, then run the API locally:

```sh
docker compose up -d postgres rabbitmq
.venv/bin/alembic upgrade head
.venv/bin/uvicorn src.api:create_app --factory
```

In another terminal, start the consumer:

```sh
.venv/bin/python -m src.worker
```

`requirements.txt` lists eight direct runtime dependencies with pinned versions.
Their supporting packages are installed automatically. `requirements-dev.txt`
adds pytest, pytest-asyncio, Ruff, mypy, and Pyright. Compatible dependency ranges
are also declared in `pyproject.toml`.

### Unit tests and static checks

```sh
.venv/bin/python -m pytest -q -m 'not integration'
.venv/bin/ruff check .
.venv/bin/ruff format --check .
.venv/bin/mypy
.venv/bin/pyright
```

mypy and Pyright check the application, tests, migrations, and examples in strict
mode. mypy uses Pydantic's plugin. VS Code settings select the local environment
and enable strict Pylance checks. Pyright's development dependency includes
Node.js, so it does not require a separate Node.js installation.

### Integration tests

These tests use real PostgreSQL and RabbitMQ. Start those services and create
dedicated test resources once:

```sh
docker compose up -d postgres rabbitmq
docker compose exec -T postgres createdb -U pays pays_test
docker compose exec -T rabbitmq rabbitmqctl add_vhost pays_test
docker compose exec -T rabbitmq rabbitmqctl set_permissions -p pays_test pays '.*' '.*' '.*'
```

Apply the migrations to the test database:

```sh
DATABASE_URL=postgresql+asyncpg://pays:pays@localhost:5432/pays_test \
  .venv/bin/alembic upgrade head
```

Run the full suite:

```sh
TEST_DATABASE_URL=postgresql+asyncpg://pays:pays@localhost:5432/pays_test \
TEST_RABBITMQ_URL=amqp://pays:pays@localhost:5672/pays_test \
  .venv/bin/python -m pytest -q
```

| Test variable | Default | Purpose |
| --- | --- | --- |
| `TEST_DATABASE_URL` | Unset; database tests are skipped | PostgreSQL URL whose database name ends in `_test`. |
| `TEST_RABBITMQ_URL` | Unset; broker tests are skipped | RabbitMQ URL whose vhost name ends in `_test`. |

Test fixtures read these variables from the **process environment**, not directly
from `.env`. Use the command above or export them in your shell. The fixtures clear
the test database tables and queues before testing. Example URLs use the default
credentials; adjust them if you configured different passwords.

Check that database migrations match the SQLAlchemy models:

```sh
.venv/bin/alembic check
```

## GitHub CI/CD

[`.github/workflows/ci.yml`](.github/workflows/ci.yml) runs on pull requests,
pushes to `main`, version tags beginning with `v`, and manual runs from the
repository's **Actions** tab.

Each run:

1. Checks dependency compatibility, Ruff linting and formatting, and strict mypy
   and Pyright types.
2. Runs all unit and integration tests on Python 3.12 and 3.14 with fresh
   PostgreSQL 17 and RabbitMQ 4.1 services. Test credentials are disposable and
   do not require your `.env` file.
3. Applies migrations, checks for differences from the models, and verifies a
   downgrade to the empty schema followed by an upgrade.
4. Builds the Docker image after the checks pass, then verifies application
   imports and the packaged migration files inside it.
5. Publishes the image to GitHub Container Registry after a successful push to
   `main` or a `v*` tag.

JUnit test reports remain available as workflow artifacts for 14 days, including
when tests fail. pip and Docker build caches speed up later runs. Action versions
are pinned to commit hashes; [Dependabot](.github/dependabot.yml) opens weekly
update PRs for actions, Python dependencies, and the Docker base image.

### Enable the pipeline

Commit these files and push the project to a GitHub repository with `main` as its
default branch. Enable GitHub Actions if your repository or organization has
disabled it, and allow the workflow to write packages. The publication job
requests `packages: write` and authenticates with GitHub's generated
`GITHUB_TOKEN`; no custom repository secrets are needed. See
[GitHub's image publishing documentation](https://docs.github.com/en/actions/tutorials/publish-packages/publish-docker-images).

To require CI before merging, add **Lint and type checks**, **Tests (Python 3.12)**,
**Tests (Python 3.14)**, and **Build Docker image** as required status checks in
your branch protection rule or repository ruleset. The publishing job only runs
after those checks succeed on a push.

### Published images

Images use `ghcr.io/<owner>/<repository>`; the image path is lowercase. Replace
the placeholders with your GitHub account or organization and repository name.

| Push | Image tags |
| --- | --- |
| `main` | `main`, `latest`, and `sha-<full-commit-sha>` |
| `v0.1.0` | `v0.1.0` and `sha-<full-commit-sha>` |

Version tags do not overwrite `latest`. Pull request and manual runs validate and
build the image without publishing it.

For example, create a release image with:

```sh
git tag v0.1.0
git push origin v0.1.0
```

Then pull it on your deployment host:

```sh
docker pull ghcr.io/<owner>/<repository>:v0.1.0
```

Private packages require an authenticated Docker login with package read access.
The pipeline delivers container images; starting or updating API and consumer
containers on a server requires a deployment target and its configuration.

## How processing works

The API accepts requests in the `api` container. The `consumer` container runs
two concurrent tasks: an **outbox publisher** and a **payment consumer**.
PostgreSQL stores payments and outbox events; RabbitMQ carries events between
publishing and processing.

### Accept a payment and publish its event

This diagram follows a new, valid payment request. Returning `202` and processing
the saved event are separate paths after the creation transaction commits.

```mermaid
flowchart TB
    REQUEST["Client: POST /api/v1/payments"]
    API["API: authenticate and validate<br/>Check Idempotency-Key"]
    CREATE[("PostgreSQL: commit one transaction<br/>Payment with status pending<br/>Outbox event with event_id and payment_id")]
    ACCEPTED["Client receives 202<br/>payment_id, status, created_at"]
    PUBLISHER["Worker: outbox publisher<br/>Read due unpublished events"]
    PUBLISH["Publish a persistent event<br/>Exchange: payments<br/>Routing key: payments.new"]
    CONFIRMED{"RabbitMQ confirms publication?"}
    PUBLISHED[("PostgreSQL: set published_at<br/>Commit the outbox update")]
    BACKOFF["Keep the event unpublished<br/>Retry after 1, 2, 4, ... up to 60 seconds"]
    QUEUE["RabbitMQ queue: payments.new<br/>Continue with the consumer diagram below"]

    REQUEST --> API --> CREATE
    CREATE --> ACCEPTED
    CREATE --> PUBLISHER --> PUBLISH
    PUBLISH --> CONFIRMED
    PUBLISH -.->|Broker accepts the message| QUEUE
    CONFIRMED -->|Yes| PUBLISHED
    CONFIRMED -->|Error or timeout| BACKOFF
    BACKOFF --> PUBLISHER

    classDef database fill:#e8f0fe,stroke:#4c78a8,color:#111827
    class CREATE,PUBLISHED database
```

The outbox is the durable record that a payment's event still needs publishing.
If RabbitMQ is unavailable, the API can still accept payments while PostgreSQL
is available. Publication retries have no attempt limit and do not use the
consumer's retry queues or DLQ.

The dotted arrow represents asynchronous message delivery. Once RabbitMQ accepts
the event, the consumer can receive it before the publisher commits `published_at`.
A crash in that interval can cause another publication with the same event ID.

### Process the payment and deliver its notification

RabbitMQ delivers one message at a time to one active consumer. Payment processing
and webhook delivery have separate saved state, so a repeated message can skip
work that has already finished.

```mermaid
flowchart TB
    RECEIVE["Receive from payments.new"]
    PROCESS["Validate the event and lock the payment<br/>If pending: simulate for 2-5 seconds and commit the result<br/>If already final: reuse the saved result"]
    DELIVERED{"Webhook already delivered?"}
    ATTEMPTS{"Recorded webhook attempts below 4?"}
    WEBHOOK["POST the saved result to webhook_url<br/>Use event_id as Idempotency-Key"]
    RESPONSE{"HTTP response is 2xx?"}
    SUCCESS[("PostgreSQL: commit attempt and delivery time<br/>Clear the delivery error")]
    ERROR[("PostgreSQL: commit attempt and delivery error<br/>Keep the final payment status")]
    BUDGET{"Message attempt below 4?"}
    RETRY["Publish the next attempt<br/>Exchange: payments.retry<br/>Queue: payments.retry.1s, .2s, or .4s"]
    DLQ["Publish the failed event<br/>Exchange: payments.dlx<br/>Queue: payments.dlq"]
    TRANSFER{"RabbitMQ confirms the transfer?"}
    ACK["ACK the original message"]
    NACK["Wait 1 second, then NACK<br/>Requeue the original message"]

    RECEIVE --> PROCESS --> DELIVERED
    DELIVERED -->|Yes| ACK
    DELIVERED -->|No| ATTEMPTS
    ATTEMPTS -->|Yes| WEBHOOK --> RESPONSE
    ATTEMPTS -->|No| DLQ
    RESPONSE -->|Yes| SUCCESS --> ACK
    RESPONSE -->|No| ERROR --> BUDGET
    WEBHOOK -->|Timeout or transport error| ERROR
    PROCESS -->|Processing exception| BUDGET
    BUDGET -->|Yes| RETRY
    BUDGET -->|No| DLQ
    RETRY --> TRANSFER
    DLQ --> TRANSFER
    TRANSFER -->|Yes| ACK
    TRANSFER -->|Error or timeout| NACK
    NACK --> RECEIVE
    RETRY -.->|After its TTL, via the payments exchange| RECEIVE

    classDef database fill:#e8f0fe,stroke:#4c78a8,color:#111827
    classDef completed fill:#e8f5e9,stroke:#2e7d32,color:#111827
    classDef failed fill:#ffebee,stroke:#c62828,color:#111827
    class SUCCESS,ERROR database
    class ACK completed
    class DLQ failed
```

`succeeded` and `failed` are both final payment results. A gateway decline follows
the normal notification path. A failed HTTP notification retries delivery of that
saved result. If processing raises an exception before a result is committed,
the payment remains `pending` and the next attempt can process it again.

Normal error handling allows **four total message attempts**: the initial attempt
plus three retries delayed by 1, 2, and 4 seconds. A separate count of committed
webhook attempts prevents a repeated event from starting a fifth recorded delivery.
Persistence errors also enter the consumer's error handling; a failed database
commit can leave a delivery unrecorded, as described under recovery below.

### How the diagrams match the implementation

| Diagram step | Current service behavior | Code |
| --- | --- | --- |
| Accept and save | Saves the payment and one outbox event in a single transaction. Same key and data reuse the existing payment with `202`; different data returns `409`. | [payments.py](src/payments.py), [api.py](src/api.py) |
| Publish the event | Selects due events with `FOR UPDATE SKIP LOCKED`, publishes with confirmations, then commits `published_at`. Failed publications remain in the outbox with backoff. | [outbox.py](src/outbox.py) |
| Share one worker process | Starts the publisher as a background task and the consumer as a RabbitMQ subscriber in the same `consumer` container. | [worker.py](src/worker.py), [compose.yaml](compose.yaml) |
| Save the payment result | Locks the payment row, processes only `pending` payments, and commits the final status before sending a webhook. | [processing.py](src/processing.py), `finish_payment()` |
| Deliver the notification | Skips delivered webhooks, checks the four-attempt limit, sends the saved result, and commits delivery state. | [processing.py](src/processing.py), `send_webhook()` |
| Retry, dead-letter, or acknowledge | Increments the message attempt when publishing a retry. Acknowledges success or a confirmed transfer; requeues the original message if the transfer fails. | [consumer.py](src/consumer.py) |
| Delay and route events | Defines durable quorum queues, three exchanges, 1/2/4-second TTLs, dead-letter routing, and one active consumer with `prefetch_count=1`. | [messaging.py](src/messaging.py) |

### Delivery and recovery

- Outbox publishing retries indefinitely, with delays growing from 1 to 60 seconds.
  This is separate from the consumer's four-attempt limit.
- Messages and queues are durable. Retry delays are stored in RabbitMQ, so they
  survive consumer restarts and leave the consumer free to process other messages.
- Delivery is **at least once**: a crash can cause an event or notification to
  arrive again. Receivers should deduplicate by `event_id`.
- Payment row locks prevent concurrent duplicate processing. The publisher uses
  `FOR UPDATE SKIP LOCKED` so multiple publishers can share the outbox safely.
- A notification accepted just before a crash can be sent again if its delivery
  record was not committed. Crash redeliveries can exceed four physical requests.
- Retry and dead-letter transfers wait for RabbitMQ confirmation. If a transfer
  fails, the original message is requeued. A separate broker limit of 1,000
  interrupted deliveries sends persistent crash loops to the dead-letter queue.
- RabbitMQ uses durable quorum queues, confirmed publications, and at-least-once
  dead-lettering. `payments.new` allows one active consumer with one message in
  flight. The dead-letter exchange is `payments.dlx`.

A real payment gateway would also need a stable gateway idempotency key and a
way to reconcile uncertain outcomes. Database locks alone cannot prevent duplicate
external charges. Webhook URLs come from trusted API clients; restrict outbound
network access when deploying the service for untrusted clients.

### Code map

| File | Responsibility |
| --- | --- |
| `src/api.py` | HTTP endpoints and API-key authentication |
| `src/schemas.py` | Request, response, event, and webhook schemas |
| `src/models.py` | Payment and outbox tables |
| `src/payments.py` | Atomic creation and idempotency |
| `src/processing.py` | Simulated gateway and webhook delivery |
| `src/consumer.py` | Message acknowledgements, retries, and dead letters |
| `src/messaging.py` | RabbitMQ queues and exchanges |
| `src/outbox.py` | Publication of waiting events |
| `src/worker.py` | Consumer and publisher lifecycle |
| `src/config.py` | Environment settings |
| `src/database.py` | Async database connections and sessions |
| `migrations/` | Alembic schema migrations |
