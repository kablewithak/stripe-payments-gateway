# 🚀 Quick Start Guide - Payment System

## ✅ What Was Built

A **production-grade distributed payment processing system** with Stripe integration that includes:

### Core Components
1. **Payment Processor** - Full payment lifecycle management with distributed locking
2. **Idempotency System** - Prevents duplicate charges using Redis + Database
3. **Stripe Integration** - Test Mode API with retry logic and circuit breaker
4. **Webhook Handler** - Signature verification and event deduplication
5. **Reconciliation Engine** - Daily reconciliation with Stripe reports
6. **Transactional Outbox** - Exactly-once message delivery
7. **Saga Orchestration** - Complex workflow management
8. **Monitoring** - Prometheus metrics, structured logging, health checks

### Project Structure
```
payment-systems/
├── api/                    # FastAPI application
│   ├── main.py            # Main app with middleware
│   ├── routes.py          # API endpoints
│   └── schemas.py         # Pydantic models
├── core/                   # Business logic
│   ├── payment_processor.py    # Main orchestrator
│   ├── idempotency.py          # Idempotency manager
│   ├── reconciliation.py       # Reconciliation engine
│   ├── outbox.py              # Outbox publisher
│   └── saga.py                # Saga orchestrator
├── integrations/          # External services
│   ├── stripe_client.py   # Stripe API wrapper
│   └── webhook_handler.py # Webhook processor
├── database/              # Data layer
│   ├── models.py          # SQLAlchemy models
│   ├── connection.py      # DB connection
│   └── migrations/        # Alembic migrations
├── monitoring/            # Observability
│   ├── metrics.py         # Prometheus metrics
│   ├── logging.py         # Structured logging
│   └── health.py          # Health checks
├── workers/               # Background workers
│   ├── outbox_publisher.py
│   └── reconciliation_worker.py
├── tests/                 # Test suite
│   ├── test_payment_processor.py
│   ├── test_integration.py
│   ├── test_race_conditions.py
│   └── load_test.py
├── docker-compose.yml     # All services
└── README.md             # Full documentation
```

## 🎯 5-Minute Setup

### 1. Get Stripe Test Keys (2 minutes)

```bash
# 1. Sign up at https://stripe.com (free)
# 2. Go to: Dashboard → Developers → API Keys
# 3. Copy both test keys:
#    - sk_test_... (Secret Key)
#    - pk_test_... (Publishable Key)
```

### 2. Configure Environment

```bash
cd payment-systems
cp .env.example .env

# Edit .env with your keys:
STRIPE_SECRET_KEY=sk_test_your_key_here
STRIPE_PUBLISHABLE_KEY=pk_test_your_key_here
```

### 3. Start Services (1 minute)

```bash
# Start PostgreSQL, Redis, RabbitMQ, Prometheus, Grafana
docker-compose up -d

# Wait for services to be healthy
docker-compose ps
```

### 4. Install & Run

```bash
# Create virtual environment
python -m venv venv
source venv/bin/activate  # Windows: venv\Scripts\activate

# Install dependencies
pip install -r requirements.txt

# Run migrations
alembic upgrade head

# Start API
uvicorn api.main:app --reload

# In separate terminals:
python -m workers.outbox_publisher
python -m workers.reconciliation_worker
```

### 5. Test It Works! (1 minute)

```bash
# Check health
curl http://localhost:8000/health

# Create test payment
curl -X POST http://localhost:8000/payments \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "test_user_123",
    "amount_cents": 1000,
    "currency": "USD"
  }'

# View API docs
open http://localhost:8000/docs

# View metrics
open http://localhost:9090  # Prometheus
open http://localhost:3000  # Grafana (admin/admin)
```

## 🧪 Testing

### Unit Tests
```bash
pytest tests/ -m unit -v
```

### Integration Tests
```bash
pytest tests/ -m integration -v
```

### Load Tests
```bash
# Open browser: http://localhost:8089
locust -f tests/load_test.py --host=http://localhost:8000
# Configure: 100 users, 10 spawn rate
```

### Race Condition Tests
```bash
pytest tests/ -m race -v
```

## 💳 Using Stripe Test Mode

### Test Card Numbers

**Success:**
- `4242 4242 4242 4242` - Visa (always succeeds)
- `5555 5555 5555 4444` - Mastercard

**Failures:**
- `4000 0000 0000 0002` - Card declined
- `4000 0000 0000 9995` - Insufficient funds

**For all cards:**
- Expiry: Any future date (e.g., `12/34`)
- CVC: Any 3 digits (e.g., `123`)
- ZIP: Any zip code

### Webhook Testing

```bash
# Install Stripe CLI
brew install stripe/stripe-cli/stripe

# Login
stripe login

# Forward webhooks
stripe listen --forward-to localhost:8000/webhooks/stripe

# Copy the webhook secret (whsec_...) to .env

# Test events
stripe trigger payment_intent.succeeded
stripe trigger payment_intent.payment_failed
```

## 📊 What to Check

### 1. Stripe Dashboard
- Go to: https://dashboard.stripe.com/test/payments
- See all test PaymentIntents created
- Check webhook events: Dashboard → Developers → Events

### 2. Application Logs
```bash
docker-compose logs -f api
# Look for:
# - payment_creation_started
# - idempotency_key_generated
# - payment_lock_acquired
# - stripe_payment_intent_created
# - payment_created_successfully
```

### 3. Database
```bash
docker exec -it payment-systems-postgres psql -U postgres -d payments_db

# Check payments
SELECT id, user_id, amount_cents, status, created_at FROM payments;

# Check events
SELECT event_type, created_at FROM payment_events ORDER BY created_at DESC LIMIT 10;
```

### 4. Redis Cache
```bash
docker exec -it payment-systems-redis redis-cli

# Check idempotency keys
KEYS idempotency:*

# Check locks
KEYS payment:lock:*
```

### 5. Metrics
```bash
# Prometheus: http://localhost:9090
# Try these queries:

# Payment success rate
rate(payment_requests_total{status="succeeded"}[5m])

# p95 latency
histogram_quantile(0.95, payment_processing_duration_seconds)

# Idempotency cache hits
idempotency_cache_hits_total
```

## 🎓 Key Concepts Demonstrated

### 1. Idempotency
- Same request → Same response (no duplicate charges)
- Implemented with Redis cache + Database fallback
- Key format: `{user_id}:{payment_hash}:{timestamp_hash}`

### 2. Distributed Locking
- Prevents race conditions across multiple API instances
- Uses Redlock algorithm with Redis
- 30-second timeout with automatic release

### 3. Transactional Outbox
- Writes events to DB in same transaction as payment
- Background worker publishes to message queue
- Guarantees exactly-once delivery

### 4. Retry Logic
- Exponential backoff: 1s, 2s, 4s, 8s, 16s
- Max 5 retries for transient errors
- Circuit breaker prevents cascade failures

### 5. Webhook Handling
- Signature verification using Stripe webhook secret
- Event deduplication (stores processed IDs in Redis)
- Async processing with proper error handling

### 6. Reconciliation
- Daily comparison of Stripe reports vs database
- Detects missing payments and amount mismatches
- Automatic retry for failed transactions

## 🐛 Troubleshooting

### Services won't start
```bash
docker-compose down -v  # Remove volumes
docker-compose up -d
```

### Database migrations fail
```bash
alembic downgrade base
alembic upgrade head
```

### Stripe API errors
```bash
# Check API key format
echo $STRIPE_SECRET_KEY  # Should start with sk_test_

# Test Stripe CLI
stripe listen --print-secret
```

### Can't connect to services
```bash
# Check all services are running
docker-compose ps

# Check logs
docker-compose logs postgres
docker-compose logs redis
```

## 📚 Next Steps

1. **Understand the Code**
   - Read `core/payment_processor.py` - Main orchestration logic
   - Study `core/idempotency.py` - Idempotency implementation
   - Review `integrations/stripe_client.py` - Retry and circuit breaker

2. **Run Load Tests**
   - Test with 100 concurrent users
   - Verify no duplicate payments
   - Check Prometheus metrics

3. **Explore Stripe Dashboard**
   - View all test transactions
   - Check webhook delivery
   - Review event logs

4. **Experiment**
   - Try different test cards
   - Trigger webhook events
   - Test idempotency (send same request twice)
   - Monitor metrics in Grafana

5. **Extend (Optional)**
   - Add user authentication
   - Implement subscription billing
   - Add fraud detection
   - Create admin dashboard

## ✅ Success Criteria Checklist

- [ ] All services running (`docker-compose ps`)
- [ ] Health check passes (`curl http://localhost:8000/health`)
- [ ] Can create payment via API
- [ ] Payment appears in Stripe Dashboard
- [ ] Webhooks processed successfully
- [ ] Metrics visible in Prometheus
- [ ] Tests pass (`make test`)
- [ ] Load test completes (`make test-load`)
- [ ] No duplicate payments under concurrent load
- [ ] Reconciliation job runs successfully

## 🎉 What You've Accomplished

You now have a **production-grade payment system** that demonstrates:

- ✅ Financial systems best practices
- ✅ Distributed systems patterns
- ✅ Comprehensive testing strategies
- ✅ Production-ready monitoring
- ✅ Stripe integration expertise
- ✅ Database design for financial data
- ✅ Async Python with FastAPI
- ✅ Docker containerization
- ✅ CI/CD ready codebase

This is **portfolio-ready** and demonstrates understanding of:
- Payment processing
- Distributed systems
- Microservices architecture
- Production observability
- Testing strategies
- Infrastructure as code

## 📞 Need Help?

1. Check logs: `docker-compose logs -f`
2. Review README.md for full documentation
3. Check Stripe Dashboard: https://dashboard.stripe.com/test
4. Test health endpoint: `curl http://localhost:8000/health`
5. Review metrics: http://localhost:9090

---

**🎓 You've built a production-grade payment system! Time to test and explore.**
