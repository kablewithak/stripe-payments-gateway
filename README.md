# Stripe Payments Gateway

Production-style payment gateway demo built with **FastAPI**, **PostgreSQL**, **Redis**, and **Stripe**.

This project focuses on the parts that actually matter in payment systems:

- **idempotent payment creation**
- **distributed locking**
- **webhook verification + deduplication**
- **auditability through payment events**
- **refund handling**
- **reconciliation hooks**
- **health checks and metrics**
- **deterministic test coverage**

At the time of writing, the project has a Current status: 59 passing tests, 3 intentionally skipped legacy race-condition tests across unit and integration coverage.

---

## What this project does

This system exposes an API for creating and managing payments while enforcing the core backend controls you would expect in a real payment workflow:

- prevent duplicate payment creation for the same request
- persist payment state transitions
- integrate with Stripe for payment intents and refunds
- process Stripe webhooks safely
- keep an immutable audit trail of payment events
- expose health and metrics endpoints for operations

This is not just a CRUD API with Stripe glued on top. It is a backend reliability demo.

---

## Core capabilities

### Payment creation
`POST /payments`

Creates a payment with:

- request validation
- idempotency key generation
- duplicate request replay
- Redis-backed lock acquisition
- persisted payment row + event history
- Stripe payment intent creation
- stored response snapshot

### Payment status
`GET /payments/{payment_id}`

Returns the latest persisted payment status and metadata.

### Refunds
`POST /payments/{payment_id}/refund`

Creates a full or partial refund for a completed payment.

### Stripe webhooks
`POST /webhooks/stripe`

Handles Stripe webhook delivery with:

- signature verification
- deduplication
- event-specific handlers
- audit logging
- graceful processing semantics

### Reconciliation
`POST /admin/reconcile`

Manual reconciliation hook for comparing internal state against Stripe state for a given date.

### Monitoring
- `GET /health`
- `GET /health/live`
- `GET /health/ready`
- `GET /metrics`

---

## Architecture overview

```text
Client
  │
  ▼
FastAPI Routes
  │
  ▼
Payment Processor / Webhook Handler
  │
  ├── PostgreSQL
  │     ├── payments
  │     ├── payment_events
  │     └── outbox_events
  │
  ├── Redis
  │     ├── idempotency response cache
  │     ├── payment locks
  │     └── webhook dedup markers
  │
  └── Stripe
        ├── payment intents
        ├── refunds
        └── webhook signatures/events
Design choices
1. Idempotency first

Payment creation is designed so repeated identical requests return the same result instead of creating duplicate charges.

2. Locking before side effects

Redis is used to avoid concurrent processing of the same logical payment request.

3. Snapshot + audit trail

The payments table stores the current state.
The payment_events table stores the immutable history.

This gives you:

fast status reads
operational traceability
safer debugging
a better demo story
4. Graceful degradation

Where sensible, infrastructure failures are handled in a controlled way rather than collapsing the whole flow instantly.

5. Test determinism

Route-level tests patch dependency-heavy components so the test suite stays stable and demo-safe.

Request lifecycle
Create payment flow
Validate request
Generate idempotency key
Check prior stored response
Acquire payment lock
Persist payment in pending/processing state
Create Stripe payment intent
Persist final state + audit events
Cache response for future idempotent replay
Release lock
Webhook flow
Receive raw Stripe event
Verify Stripe signature
Check Redis dedup key
Route to registered handler
Update payment snapshot
Persist audit event
Mark webhook as processed
Refund flow
Load payment
Validate refund eligibility
Call Stripe refund API
Persist refund status + audit event
Return refund result
API summary
Create payment

POST /payments

Example body:

{
  "user_id": "123e4567-e89b-12d3-a456-426614174000",
  "amount_cents": 1000,
  "currency": "USD",
  "metadata": {
    "order_id": "order_123"
  }
}
Get payment status

GET /payments/{payment_id}

Refund payment

POST /payments/{payment_id}/refund

Example body:

{
  "amount_cents": 500,
  "reason": "requested_by_customer"
}
Stripe webhook

POST /webhooks/stripe

Requires Stripe-Signature header.

Health and metrics
GET /health
GET /health/live
GET /health/ready
GET /metrics
Local setup
Requirements
Python 3.11+
PostgreSQL
Redis
Stripe test keys
1. Clone the repo
git clone <your-repo-url>
cd stripe-payments-gateway
2. Create and activate virtual environment
python -m venv .venv
.\.venv\Scripts\Activate.ps1
3. Install dependencies
pip install -r requirements.txt
4. Set environment variables

Create a .env file or export variables in PowerShell.

Minimum required values:

DATABASE_URL=postgresql+asyncpg://payments_test_user:payments_test_pw@127.0.0.1:5432/payments_dev
TEST_DATABASE_URL=postgresql+asyncpg://payments_test_user:payments_test_pw@127.0.0.1:5432/payments_test
REDIS_URL=redis://localhost:6379/0
STRIPE_SECRET_KEY=sk_test_...
STRIPE_WEBHOOK_SECRET=whsec_...
5. Make sure dependencies are running
PostgreSQL

If PostgreSQL is installed as a Windows service, it does not need to run in a separate terminal. It just needs to be running in the background.

Check it with:

Get-Service *postgres*

Start it if needed:

Start-Service postgresql-x64-17
Redis

Make sure Redis is running locally on the configured port.

6. Run the API
python -m uvicorn api.main:app --reload

Open:

API docs: http://127.0.0.1:8000/docs
Metrics: http://127.0.0.1:8000/metrics
Running tests
Full suite
python -m pytest -v
Focused backend suite
python -m pytest tests/test_day1_core.py tests/test_payment_processor.py tests/test_webhook_handler.py tests/test_integration.py tests/test_refund_and_webhook_routes.py tests/test_health_routes.py -v -x
If tests hang

The most common causes are:

PostgreSQL not running
stale environment variables in a new PowerShell session
a malformed test file causing collection failure before execution

Useful checks:

Get-Service *postgres*
python -c "import os; print(os.getenv('DATABASE_URL')); print(os.getenv('TEST_DATABASE_URL'))"
Project structure
api/
  main.py
  routes.py
  schemas.py

core/
  payment_processor.py
  idempotency.py
  reconciliation.py

database/
  connection.py
  models.py

integrations/
  stripe_client.py
  webhook_handler.py

monitoring/
  health.py
  metrics.py

tests/
  test_day1_core.py
  test_payment_processor.py
  test_webhook_handler.py
  test_integration.py
  test_refund_and_webhook_routes.py
  test_health_routes.py
  conftest.py
Reliability features
Idempotency

The gateway generates a deterministic idempotency key from the payment request so duplicate payment requests can return the same logical result.

Redis lock lifecycle

Redis is used to reduce concurrent duplicate processing risk for the same payment request.

Webhook deduplication

Webhook event IDs are tracked so duplicate Stripe deliveries do not reapply the same state transition repeatedly.

Audit trail

Payment state changes are paired with immutable event records to preserve operational history.

Health checks

Health endpoints cover:

database reachability
Redis reachability
Stripe reachability
liveness/readiness separation
Metrics

Prometheus-compatible metrics are exposed for monitoring.

Testing strategy

The test suite is split across:

unit tests for processor and handler logic
integration tests for HTTP route contracts
deterministic health-route tests that avoid live dependency flakiness

Key behavior covered includes:

validation failures
successful payment creation
permanent vs transient provider failures
idempotent replay
lock conflicts
webhook deduplication
refund contract behavior
health/readiness/liveness routes
Demo walkthrough

The default test suite is demo-safe and deterministic.

Race-condition stress tests are intentionally skipped in normal local runs because they require isolated DB sessions per concurrent task and a dedicated Redis-backed concurrency setup.

A clean demo flow is:

1. Create a payment

Use POST /payments with a valid request.

2. Fetch the status

Use GET /payments/{payment_id} to show the stored payment state.

3. Explain idempotency

Repeat the same logical request and explain how duplicate work is prevented.

4. Simulate webhook processing

Show how webhook success/failure/refund updates the payment lifecycle while keeping an audit trail.

5. Trigger a refund

Use POST /payments/{payment_id}/refund.

6. Show health and metrics

Open /health, /health/ready, and /metrics.

7. Explain why this matters

Highlight:

duplicate prevention
safer payment state handling
webhook correctness
observability
test coverage
Known limitations

This is a demo system, not a finished commercial gateway.

Current limitations may include:

no full async outbox publisher/consumer path yet
no full authentication/authorization layer shown here
reconciliation is present as a manual/admin hook rather than a scheduled production workflow
deployment and secrets management may still need production hardening
Stripe integration is expected to run in test mode for local development
Security and operational notes
keep Stripe secrets out of logs and source control
use separate development and test databases
do not reuse production credentials locally
prefer environment-based configuration
keep payment event history immutable
verify webhook signatures before processing
use least privilege for database and infrastructure access
Why this project matters

This project demonstrates more than “I can call Stripe.”

It shows:

backend reliability thinking
idempotent API design
operational safety
payment lifecycle modeling
auditability
realistic test discipline

That makes it a stronger portfolio artifact than a thin CRUD wrapper around a payment SDK.