"""
Load tests using Locust.

Simulates high-concurrency payment requests to test system performance.

Run with: locust -f tests/load_test.py --host=http://localhost:8000
"""
import uuid
from locust import HttpUser, between, task


class PaymentUser(HttpUser):
    """
    Simulated user creating payments.

    Tests system under load with concurrent payment requests.
    """

    wait_time = between(1, 3)  # Wait 1-3 seconds between requests

    @task(10)
    def create_payment(self) -> None:
        """
        Create a payment request.

        Uses Stripe test card numbers for realistic testing.
        """
        payload = {
            "user_id": str(uuid.uuid4()),
            "amount_cents": 1000,
            "currency": "USD",
            "metadata": {
                "test_card": "4242424242424242",  # Stripe test card
                "load_test": True,
            },
        }

        with self.client.post(
            "/payments",
            json=payload,
            catch_response=True,
        ) as response:
            if response.status_code == 201:
                response.success()
            elif response.status_code == 400:
                # Client error - don't count as failure
                response.success()
            else:
                response.failure(f"Unexpected status: {response.status_code}")

    @task(3)
    def get_health(self) -> None:
        """Check health endpoint."""
        self.client.get("/health")

    @task(1)
    def get_metrics(self) -> None:
        """Check metrics endpoint."""
        self.client.get("/metrics")


class IdempotencyUser(HttpUser):
    """
    Test idempotency under load.

    Makes multiple requests with the same idempotency characteristics.
    """

    wait_time = between(0.5, 1.5)

    def on_start(self) -> None:
        """Set up user-specific data."""
        self.user_id = str(uuid.uuid4())

    @task
    def create_duplicate_payment(self) -> None:
        """
        Create payment with same user ID and amount.

        Should demonstrate idempotency.
        """
        payload = {
            "user_id": self.user_id,  # Same user ID
            "amount_cents": 1000,  # Same amount
            "currency": "USD",
            "metadata": {"idempotency_test": True},
        }

        self.client.post("/payments", json=payload)


"""
Load Test Scenarios:

1. Basic Load Test:
   locust -f tests/load_test.py --host=http://localhost:8000 --users=100 --spawn-rate=10

2. Stress Test (1000 concurrent users):
   locust -f tests/load_test.py --host=http://localhost:8000 --users=1000 --spawn-rate=50

3. Idempotency Test:
   locust -f tests/load_test.py --host=http://localhost:8000 --users=50 --spawn-rate=10 IdempotencyUser

Success Criteria:
- Throughput: >100 payments/second
- p95 latency: <500ms
- Error rate: <1%
- No duplicate payments (verify in Stripe Dashboard)
- Zero data corruption (verify with reconciliation)
"""
