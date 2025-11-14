"""
Prometheus metrics for payment system monitoring.

Tracks:
- Payment request counts by status
- Payment processing duration
- Payment amounts
- Idempotency cache hits
- Stripe API errors
- Reconciliation discrepancies
- Outbox queue depth
"""
from prometheus_client import Counter, Gauge, Histogram

# Payment metrics
payment_requests_total = Counter(
    "payment_requests_total",
    "Total number of payment requests",
    ["status", "currency"],
)

payment_processing_duration_seconds = Histogram(
    "payment_processing_duration_seconds",
    "Payment processing duration in seconds",
    buckets=(0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0),
)

payment_amount_cents = Histogram(
    "payment_amount_cents",
    "Payment amounts in cents",
    buckets=(50, 100, 500, 1000, 5000, 10000, 50000, 100000, 500000, 1000000),
)

# Idempotency metrics
idempotency_cache_hits_total = Counter(
    "idempotency_cache_hits_total",
    "Total idempotency cache hits",
    ["source"],  # redis, database, miss
)

# Stripe API metrics
stripe_api_requests_total = Counter(
    "stripe_api_requests_total",
    "Total Stripe API requests",
    ["operation", "status"],  # operation: create_intent, retrieve, etc.
)

stripe_api_errors_total = Counter(
    "stripe_api_errors_total",
    "Total Stripe API errors",
    ["error_type"],  # transient, permanent, rate_limit
)

stripe_api_duration_seconds = Histogram(
    "stripe_api_duration_seconds",
    "Stripe API call duration in seconds",
    ["operation"],
    buckets=(0.1, 0.25, 0.5, 0.75, 1.0, 2.5, 5.0, 7.5, 10.0),
)

# Circuit breaker metrics
stripe_circuit_breaker_state = Gauge(
    "stripe_circuit_breaker_state",
    "Stripe circuit breaker state (0=closed, 1=open, 2=half_open)",
)

# Webhook metrics
webhook_events_received_total = Counter(
    "webhook_events_received_total",
    "Total webhook events received",
    ["event_type"],
)

webhook_events_processed_total = Counter(
    "webhook_events_processed_total",
    "Total webhook events processed",
    ["event_type", "status"],  # success, failed, duplicate
)

webhook_processing_duration_seconds = Histogram(
    "webhook_processing_duration_seconds",
    "Webhook processing duration in seconds",
    ["event_type"],
    buckets=(0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Reconciliation metrics
reconciliation_discrepancies_total = Gauge(
    "reconciliation_discrepancies_total",
    "Total reconciliation discrepancies",
)

reconciliation_discrepancy_cents = Gauge(
    "reconciliation_discrepancy_cents",
    "Reconciliation discrepancy amount in cents",
)

reconciliation_duration_seconds = Histogram(
    "reconciliation_duration_seconds",
    "Reconciliation job duration in seconds",
    buckets=(10, 30, 60, 120, 300, 600, 1800),
)

reconciliation_last_run_timestamp = Gauge(
    "reconciliation_last_run_timestamp",
    "Timestamp of last reconciliation run",
)

# Outbox metrics
outbox_queue_depth = Gauge(
    "outbox_queue_depth",
    "Number of unpublished events in outbox",
)

outbox_events_published_total = Counter(
    "outbox_events_published_total",
    "Total outbox events published",
    ["event_type"],
)

outbox_processing_duration_seconds = Histogram(
    "outbox_processing_duration_seconds",
    "Outbox batch processing duration in seconds",
    buckets=(0.1, 0.5, 1.0, 2.5, 5.0, 10.0),
)

# Database metrics
database_connections_active = Gauge(
    "database_connections_active",
    "Number of active database connections",
)

database_query_duration_seconds = Histogram(
    "database_query_duration_seconds",
    "Database query duration in seconds",
    ["operation"],
    buckets=(0.01, 0.05, 0.1, 0.25, 0.5, 1.0, 2.5, 5.0),
)

# Lock metrics
distributed_lock_acquisitions_total = Counter(
    "distributed_lock_acquisitions_total",
    "Total distributed lock acquisitions",
    ["status"],  # acquired, failed, timeout
)

distributed_lock_duration_seconds = Histogram(
    "distributed_lock_duration_seconds",
    "Distributed lock hold duration in seconds",
    buckets=(0.1, 0.5, 1.0, 5.0, 10.0, 30.0, 60.0),
)


class MetricsCollector:
    """Helper class for collecting metrics."""

    @staticmethod
    def record_payment_request(status: str, currency: str, amount_cents: int) -> None:
        """Record a payment request."""
        payment_requests_total.labels(status=status, currency=currency).inc()
        payment_amount_cents.observe(amount_cents)

    @staticmethod
    def record_payment_duration(duration_seconds: float) -> None:
        """Record payment processing duration."""
        payment_processing_duration_seconds.observe(duration_seconds)

    @staticmethod
    def record_idempotency_cache_hit(source: str) -> None:
        """Record idempotency cache hit."""
        idempotency_cache_hits_total.labels(source=source).inc()

    @staticmethod
    def record_stripe_api_call(
        operation: str, status: str, duration_seconds: float
    ) -> None:
        """Record Stripe API call."""
        stripe_api_requests_total.labels(operation=operation, status=status).inc()
        stripe_api_duration_seconds.labels(operation=operation).observe(duration_seconds)

    @staticmethod
    def record_stripe_api_error(error_type: str) -> None:
        """Record Stripe API error."""
        stripe_api_errors_total.labels(error_type=error_type).inc()

    @staticmethod
    def set_circuit_breaker_state(state: str) -> None:
        """Set circuit breaker state."""
        state_map = {"closed": 0, "open": 1, "half_open": 2}
        stripe_circuit_breaker_state.set(state_map.get(state, 0))

    @staticmethod
    def record_webhook_event(event_type: str, status: str, duration_seconds: float) -> None:
        """Record webhook event processing."""
        webhook_events_received_total.labels(event_type=event_type).inc()
        webhook_events_processed_total.labels(event_type=event_type, status=status).inc()
        webhook_processing_duration_seconds.labels(event_type=event_type).observe(
            duration_seconds
        )

    @staticmethod
    def set_reconciliation_metrics(
        discrepancies_count: int, discrepancy_cents: int, duration_seconds: float
    ) -> None:
        """Set reconciliation metrics."""
        reconciliation_discrepancies_total.set(discrepancies_count)
        reconciliation_discrepancy_cents.set(discrepancy_cents)
        reconciliation_duration_seconds.observe(duration_seconds)
        import time

        reconciliation_last_run_timestamp.set(time.time())

    @staticmethod
    def set_outbox_queue_depth(depth: int) -> None:
        """Set outbox queue depth."""
        outbox_queue_depth.set(depth)

    @staticmethod
    def record_outbox_event_published(event_type: str, duration_seconds: float) -> None:
        """Record outbox event published."""
        outbox_events_published_total.labels(event_type=event_type).inc()
        outbox_processing_duration_seconds.observe(duration_seconds)

    @staticmethod
    def record_distributed_lock(status: str, duration_seconds: float = 0) -> None:
        """Record distributed lock acquisition."""
        distributed_lock_acquisitions_total.labels(status=status).inc()
        if duration_seconds > 0:
            distributed_lock_duration_seconds.observe(duration_seconds)


# Export singleton instance
metrics = MetricsCollector()
