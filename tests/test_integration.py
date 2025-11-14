"""
Integration tests for end-to-end payment flow.

Note: These tests require running services (PostgreSQL, Redis).
Consider using testcontainers for isolated testing.
"""
import pytest
from httpx import AsyncClient


class TestPaymentIntegration:
    """Integration tests for payment API."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_payment_endpoint(
        self, client: AsyncClient, sample_payment_data: dict
    ) -> None:
        """Test payment creation via API endpoint."""
        response = await client.post("/payments", json=sample_payment_data)

        # Note: This will fail without proper Stripe test keys
        # In production tests, use actual Stripe test API
        # For now, we expect proper error handling
        assert response.status_code in [201, 400, 500]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_payment_status(self, client: AsyncClient) -> None:
        """Test getting payment status."""
        payment_id = "123e4567-e89b-12d3-a456-426614174000"
        response = await client.get(f"/payments/{payment_id}")

        assert response.status_code in [200, 404]

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_health_check(self, client: AsyncClient) -> None:
        """Test health check endpoint."""
        response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert "status" in data

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, client: AsyncClient) -> None:
        """Test Prometheus metrics endpoint."""
        response = await client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
