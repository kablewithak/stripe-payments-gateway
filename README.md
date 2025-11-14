# Production-Grade Payment Processing System

A distributed payment processing system with Stripe integration, featuring idempotency, distributed locking, webhook handling, reconciliation, and comprehensive monitoring.

## 🌟 Features

- **Stripe Test Mode Integration**: Full payment processing using Stripe's test environment
- **Idempotency**: Prevents duplicate charges using Redis-backed idempotency keys
- **Distributed Locking**: Race condition handling with Redlock algorithm
- **Webhook Processing**: Signature verification and event deduplication
- **Transactional Outbox**: Exactly-once message delivery guarantee
- **Daily Reconciliation**: Automated reconciliation with Stripe reports
- **Saga Orchestration**: Complex workflow management with compensating transactions
- **Comprehensive Monitoring**: Prometheus metrics, structured logging, health checks
- **Production-Ready**: Type hints, async/await, error handling, retry logic

## 📋 Table of Contents

- [Quick Start](#-quick-start)
- [Stripe Test Mode Setup](#-stripe-test-mode-setup)
- [Architecture](#-architecture)
- [API Documentation](#-api-documentation)
- [Testing](#-testing)
- [Monitoring](#-monitoring)
- [Production Deployment](#-production-deployment)

## 🚀 Quick Start

### Prerequisites

- Python 3.11+
- Docker & Docker Compose
- Stripe Account (free, Test Mode only)
- Make (optional)

### 1. Clone & Install

```bash
cd payment-systems
python -m venv venv
source venv/bin/activate  # On Windows: venv\Scripts\activate
pip install -r requirements.txt
```

### 2. Configure Environment

Create `.env` file from example:

```bash
cp .env.example .env
```

Edit `.env` with your Stripe test keys:

```env
STRIPE_SECRET_KEY=sk_test_your_key_here
STRIPE_PUBLISHABLE_KEY=pk_test_your_key_here
STRIPE_WEBHOOK_SECRET=whsec_your_webhook_secret
```

### 3. Start Services

```bash
# Start all services (PostgreSQL, Redis, RabbitMQ, Prometheus, Grafana)
docker-compose up -d

# Wait for services to be healthy
docker-compose ps
```

### 4. Run Migrations

```bash
# Apply database migrations
make migrate
# Or: alembic upgrade head
```

### 5. Start Application

```bash
# Start API server
make run
# Or: uvicorn api.main:app --reload

# In separate terminals, start workers:
make run-worker-outbox
make run-worker-reconciliation
```

### 6. Verify Setup

```bash
# Check health
curl http://localhost:8000/health

# View API docs
open http://localhost:8000/docs

# View metrics
curl http://localhost:8000/metrics
```

## 💳 Stripe Test Mode Setup

### Get Your Test API Keys (2 minutes)

1. **Sign up**: Go to https://stripe.com (free account, no verification needed for Test Mode)
2. **Get keys**: Dashboard → Developers → API Keys
3. **Copy both**:
   - Publishable key: `pk_test_...` (safe for frontend)
   - Secret key: `sk_test_...` (backend only)
4. **Add to `.env`**

### Test Card Numbers

Use these for testing different scenarios:

**Success:**
- `4242 4242 4242 4242` - Visa (always succeeds)
- `5555 5555 5555 4444` - Mastercard (always succeeds)

**Failures:**
- `4000 0000 0000 0002` - Card declined
- `4000 0000 0000 9995` - Insufficient funds
- `4000 0000 0000 0069` - Expired card

**Special:**
- `4000 0025 0000 3155` - Requires authentication (3D Secure)
- `4000 0000 0000 9979` - Disputed as fraudulent

**For all test cards:**
- Use any future expiry date (e.g., `12/34`)
- Use any 3-digit CVC (e.g., `123`)
- Use any billing zip code

### Webhook Testing

```bash
# Install Stripe CLI
brew install stripe/stripe-cli/stripe  # macOS
# Or: https://stripe.com/docs/stripe-cli

# Login
stripe login

# Forward webhooks to local server
stripe listen --forward-to localhost:8000/webhooks/stripe

# Copy the webhook signing secret (whsec_...) to .env
# STRIPE_WEBHOOK_SECRET=whsec_...

# Test webhooks manually
stripe trigger payment_intent.succeeded
stripe trigger payment_intent.payment_failed
stripe trigger charge.refunded
```

## 🏗️ Architecture

### System Components

```
┌─────────────────────────────────────────────────────────────┐
│                        Client/Frontend                       │
└───────────────────────────┬─────────────────────────────────┘
                            │
                            ▼
┌─────────────────────────────────────────────────────────────┐
│                      FastAPI Application                     │
│  ┌──────────────┐  ┌──────────────┐  ┌─────────────────┐  │
│  │   Payment    │  │   Webhook    │  │  Reconciliation │  │
│  │   Routes     │  │   Handler    │  │     Routes      │  │
│  └──────────────┘  └──────────────┘  └─────────────────┘  │
└───────────────────────────┬─────────────────────────────────┘
                            │
        ┌───────────────────┼───────────────────┐
        ▼                   ▼                   ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│   Payment    │    │ Idempotency  │    │   Stripe     │
│  Processor   │◄───│   Manager    │    │   Client     │
│              │    └──────────────┘    └──────┬───────┘
└──────┬───────┘                               │
       │                                       │
       ▼                                       ▼
┌──────────────┐                      ┌──────────────┐
│  PostgreSQL  │                      │Stripe API    │
│              │                      │(Test Mode)   │
└──────────────┘                      └──────────────┘
       │
       ▼
┌──────────────┐    ┌──────────────┐    ┌──────────────┐
│Transactional │───►│   Outbox     │───►│  RabbitMQ    │
│   Outbox     │    │  Publisher   │    │              │
└──────────────┘    └──────────────┘    └──────────────┘
```

### Payment Flow

1. **Request Received**: Client sends payment request
2. **Validation**: Input validated (amount, currency, user_id)
3. **Idempotency Check**: Check Redis cache, then database
4. **Distributed Lock**: Acquire lock using Redlock
5. **Database Transaction**:
   - Create payment record
   - Call Stripe API
   - Write to outbox table
   - Record audit events
6. **Transaction Commit**: Atomic commit of all changes
7. **Lock Release**: Release distributed lock
8. **Response**: Return payment details to client
9. **Async Processing**: Outbox publisher sends events to queue

### Database Schema

**Payments Table:**
- Stores all payment transactions
- Indexed by user_id, status, created_at
- Includes Stripe PaymentIntent ID

**Payment Events Table:**
- Immutable audit trail
- Every status change recorded
- Includes correlation ID for tracing

**Outbox Events Table:**
- Transactional outbox pattern
- Ensures exactly-once delivery
- Published asynchronously

**Reconciliation Status Table:**
- Daily reconciliation results
- Tracks discrepancies
- Stores detailed comparison data

## 📖 API Documentation

### Create Payment

```bash
POST /payments
Content-Type: application/json

{
  "user_id": "123e4567-e89b-12d3-a456-426614174000",
  "amount_cents": 1000,
  "currency": "USD",
  "metadata": {
    "order_id": "order_123"
  }
}

# Response (201 Created)
{
  "id": "payment_uuid",
  "user_id": "user_uuid",
  "amount_cents": 1000,
  "currency": "USD",
  "status": "requires_payment_method",
  "stripe_payment_intent_id": "pi_...",
  "idempotency_key": "...",
  "created_at": "2025-01-06T10:00:00Z"
}
```

### Get Payment Status

```bash
GET /payments/{payment_id}

# Response (200 OK)
{
  "id": "payment_uuid",
  "status": "succeeded",
  "amount_cents": 1000,
  "currency": "USD",
  ...
}
```

### Refund Payment

```bash
POST /payments/{payment_id}/refund
Content-Type: application/json

{
  "amount_cents": 500,  # Optional: partial refund
  "reason": "requested_by_customer"
}

# Response (200 OK)
{
  "payment_id": "payment_uuid",
  "refund_id": "re_...",
  "status": "succeeded",
  "amount_cents": 500
}
```

### Webhook Endpoint

```bash
POST /webhooks/stripe
Stripe-Signature: t=...,v1=...

# Stripe automatically sends events:
# - payment_intent.succeeded
# - payment_intent.payment_failed
# - charge.refunded
```

### Health & Monitoring

```bash
# Health check
GET /health

# Liveness probe
GET /health/live

# Readiness probe
GET /health/ready

# Prometheus metrics
GET /metrics
```

## 🧪 Testing

### Unit Tests

```bash
# Run all unit tests
make test-unit
# Or: pytest tests/ -m unit -v

# With coverage
pytest tests/ -m unit --cov=. --cov-report=html
```

### Integration Tests

```bash
# Requires running services
docker-compose up -d

# Run integration tests
make test-integration
# Or: pytest tests/ -m integration -v
```

### Race Condition Tests

```bash
# Test concurrent requests and distributed locking
make test-race
# Or: pytest tests/ -m race -v
```

### Load Tests

```bash
# Start application
docker-compose up -d

# Run load test with Locust
make test-load
# Or: locust -f tests/load_test.py --host=http://localhost:8000

# Open browser: http://localhost:8089
# Configure: 100 users, 10 spawn rate
# Monitor: Prometheus (http://localhost:9090)
```

**Success Criteria:**
- ✅ Throughput: >100 payments/second
- ✅ p95 latency: <500ms
- ✅ Error rate: <1%
- ✅ Zero duplicate payments
- ✅ Stripe Dashboard shows all test transactions

### Test with Real Stripe Test Mode

```bash
# Create a test payment
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test_user_123",
    "amount_cents": 1000,
    "currency": "USD"
  }'

# Check Stripe Dashboard
# Dashboard → Developers → Events
# You'll see the PaymentIntent created

# Trigger webhook event
stripe trigger payment_intent.succeeded

# Check logs - webhook should be processed
docker-compose logs -f api
```

## 📊 Monitoring

### Prometheus Metrics

Access: http://localhost:9090

**Key Metrics:**
- `payment_requests_total` - Total payments by status
- `payment_processing_duration_seconds` - Payment latency
- `idempotency_cache_hits_total` - Cache effectiveness
- `stripe_api_errors_total` - Stripe API errors
- `reconciliation_discrepancies_total` - Reconciliation issues
- `outbox_queue_depth` - Pending events

**Example Queries:**
```promql
# Payment success rate
rate(payment_requests_total{status="succeeded"}[5m])

# p95 latency
histogram_quantile(0.95, payment_processing_duration_seconds)

# Error rate
rate(stripe_api_errors_total[5m])
```

### Grafana Dashboards

Access: http://localhost:3000 (admin/admin)

**Pre-configured dashboards:**
- Payment metrics
- Stripe API health
- System performance
- Error tracking

### Structured Logging

All logs are JSON-formatted with:
- `request_id` - Request tracing
- `correlation_id` - Cross-service tracing
- `user_id` - User context
- `payment_id` - Payment context
- `timestamp` - ISO 8601 timestamp

**View logs:**
```bash
# API logs
docker-compose logs -f api

# Worker logs
docker-compose logs -f outbox-worker reconciliation-worker

# Filter by request ID
docker-compose logs api | jq 'select(.request_id=="...")'
```

## 🔧 Development

### Code Quality

```bash
# Format code
make format
# Or: black . && isort .

# Lint code
make lint
# Or: flake8 . && mypy . && pylint .

# Run all checks
make format && make lint && make test
```

### Database Migrations

```bash
# Create new migration
make migrate-create message="add_new_field"
# Or: alembic revision --autogenerate -m "add_new_field"

# Apply migrations
make migrate
# Or: alembic upgrade head

# Rollback
alembic downgrade -1
```

### Debugging

```bash
# Enable debug mode
export DEBUG=true
export LOG_LEVEL=DEBUG

# Run with debugger
python -m pdb -m uvicorn api.main:app --reload

# Check database
docker exec -it payment-systems-postgres psql -U postgres -d payments_db
# \dt - list tables
# SELECT * FROM payments;

# Check Redis
docker exec -it payment-systems-redis redis-cli
# KEYS idempotency:*
# GET idempotency:key...
```

## 🚀 Production Deployment

### Environment Variables

Required for production:

```env
# Stripe (switch to live keys)
STRIPE_SECRET_KEY=sk_live_...
STRIPE_WEBHOOK_SECRET=whsec_live_...

# Database
DATABASE_URL=postgresql+asyncpg://user:pass@host:5432/db
DATABASE_POOL_SIZE=50
DATABASE_MAX_OVERFLOW=100

# Redis
REDIS_URL=redis://host:6379/0

# Application
APP_ENV=production
LOG_LEVEL=INFO
DEBUG=false
```

### Security Checklist

- [ ] Use strong secrets (rotate regularly)
- [ ] Enable SSL/TLS for database connections
- [ ] Configure CORS properly
- [ ] Set up rate limiting
- [ ] Enable authentication/authorization
- [ ] Sanitize all logs (no secrets)
- [ ] Use Stripe live keys (production only)
- [ ] Set up webhook signature verification
- [ ] Enable Stripe Radar (fraud detection)
- [ ] Configure alerts (PagerDuty, Slack)

### Scaling Considerations

**Horizontal Scaling:**
- Run multiple API instances behind load balancer
- Distributed locks ensure consistency
- Shared Redis for idempotency cache
- Shared PostgreSQL with connection pooling

**Performance Tuning:**
- Database: Increase connection pool size
- Redis: Use Redis Cluster for high availability
- API: Tune worker count (workers = 2*CPU + 1)
- Monitoring: Set up alerts on key metrics

**High Availability:**
- PostgreSQL: Primary-replica setup with failover
- Redis: Redis Sentinel or Redis Cluster
- Load Balancer: Health check enabled
- Workers: Multiple instances with automatic restart

## 🎓 Learning Resources

### Understanding the Code

**Key Files to Study:**
1. `core/payment_processor.py` - Main payment orchestration
2. `integrations/stripe_client.py` - Stripe API with retry logic
3. `core/idempotency.py` - Idempotency implementation
4. `core/outbox.py` - Transactional outbox pattern
5. `integrations/webhook_handler.py` - Webhook processing

**Design Patterns Used:**
- Transactional Outbox Pattern
- Circuit Breaker Pattern
- Saga Orchestration Pattern
- Distributed Locking
- Idempotency Keys

### Stripe Resources

- [Stripe Test Mode Guide](https://stripe.com/docs/testing)
- [PaymentIntents API](https://stripe.com/docs/payments/payment-intents)
- [Webhook Best Practices](https://stripe.com/docs/webhooks/best-practices)
- [Idempotent Requests](https://stripe.com/docs/api/idempotent_requests)

## 📝 License

This project is for educational purposes as part of the ML Roadmap Bootcamp.

## 🤝 Contributing

This is a learning project. Feel free to experiment and extend:

**Ideas for Extension:**
- Add user authentication
- Implement subscription billing
- Add multi-currency support
- Create admin dashboard
- Add fraud detection
- Implement chargebacks handling
- Add invoice generation
- Create customer portal

## 🆘 Troubleshooting

**Database connection errors:**
```bash
# Check if PostgreSQL is running
docker-compose ps postgres

# Check logs
docker-compose logs postgres

# Restart service
docker-compose restart postgres
```

**Redis connection errors:**
```bash
# Check if Redis is running
docker-compose ps redis

# Test connection
docker exec -it payment-systems-redis redis-cli ping
```

**Stripe API errors:**
```bash
# Verify API keys in .env
echo $STRIPE_SECRET_KEY

# Check if using test keys (should start with sk_test_)
# Test Stripe CLI connection
stripe listen --print-secret
```

**Migration errors:**
```bash
# Check current version
alembic current

# Check migration history
alembic history

# Reset database (WARNING: deletes all data)
docker-compose down -v
docker-compose up -d
make migrate
```

## 📞 Support

For issues and questions:
1. Check logs: `docker-compose logs -f`
2. Review Stripe Dashboard: https://dashboard.stripe.com/test/events
3. Check health endpoints: `curl http://localhost:8000/health`
4. Review metrics: http://localhost:9090

---

**Built with ❤️ for learning production-grade payment systems**
