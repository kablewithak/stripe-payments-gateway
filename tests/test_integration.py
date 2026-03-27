"""
Integration tests for API contract behavior.
"""
from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from api import routes
from core.payment_processor import (
    PaymentConflictError,
    PaymentFailedError,
    PaymentProviderError,
    PaymentValidationError,
)


class TestPaymentIntegration:
    """Integration tests for payment API."""

    @staticmethod
    def _build_payment_response(sample_payment_data: dict) -> dict:
        """Build a valid create-payment response payload."""
        return {
            "id": str(uuid.uuid4()),
            "user_id": str(sample_payment_data["user_id"]),
            "amount_cents": sample_payment_data["amount_cents"],
            "currency": sample_payment_data["currency"],
            "status": "requires_payment_method",
            "stripe_payment_intent_id": "pi_test_123",
            "idempotency_key": f"{sample_payment_data['user_id']}:hash123",
            "created_at": "2026-03-26T10:00:00",
        }

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_payment_endpoint_returns_201_on_success(
        self,
        client: AsyncClient,
        sample_payment_data: dict,
    ) -> None:
        """Create-payment endpoint should return 201 with a valid response body."""
        mocked_payment = self._build_payment_response(sample_payment_data)

        with patch.object(
            routes.payment_processor,
            "create_payment",
            AsyncMock(return_value=mocked_payment),
        ) as mock_create_payment:
            response = await client.post("/payments", json=sample_payment_data)

        assert response.status_code == 201
        data = response.json()
        assert data["id"] == mocked_payment["id"]
        assert data["user_id"] == mocked_payment["user_id"]
        assert data["amount_cents"] == mocked_payment["amount_cents"]
        assert data["currency"] == mocked_payment["currency"]
        assert data["status"] == mocked_payment["status"]
        assert data["stripe_payment_intent_id"] == mocked_payment["stripe_payment_intent_id"]
        assert "idempotency_key" in data
        assert "created_at" in data
        mock_create_payment.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_payment_endpoint_returns_400_on_validation_error(
        self,
        client: AsyncClient,
        sample_payment_data: dict,
    ) -> None:
        """Validation failures should map to HTTP 400."""
        with patch.object(
            routes.payment_processor,
            "create_payment",
            AsyncMock(side_effect=PaymentValidationError("Amount must be positive")),
        ):
            response = await client.post("/payments", json=sample_payment_data)

        assert response.status_code == 400
        assert response.json()["detail"] == "Amount must be positive"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_payment_endpoint_returns_409_on_conflict(
        self,
        client: AsyncClient,
        sample_payment_data: dict,
    ) -> None:
        """In-progress/lock conflicts should map to HTTP 409."""
        with patch.object(
            routes.payment_processor,
            "create_payment",
            AsyncMock(side_effect=PaymentConflictError("Payment already in progress")),
        ):
            response = await client.post("/payments", json=sample_payment_data)

        assert response.status_code == 409
        assert response.json()["detail"] == "Payment already in progress"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_payment_endpoint_returns_422_on_permanent_failure(
        self,
        client: AsyncClient,
        sample_payment_data: dict,
    ) -> None:
        """Permanent payment failures should map to HTTP 422."""
        with patch.object(
            routes.payment_processor,
            "create_payment",
            AsyncMock(side_effect=PaymentFailedError("Payment failed: Card declined")),
        ):
            response = await client.post("/payments", json=sample_payment_data)

        assert response.status_code == 422
        assert response.json()["detail"] == "Payment failed: Card declined"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_create_payment_endpoint_returns_503_on_provider_error(
        self,
        client: AsyncClient,
        sample_payment_data: dict,
    ) -> None:
        """Transient upstream/provider failures should map to HTTP 503."""
        with patch.object(
            routes.payment_processor,
            "create_payment",
            AsyncMock(side_effect=PaymentProviderError("Payment provider error: Stripe timeout")),
        ):
            response = await client.post("/payments", json=sample_payment_data)

        assert response.status_code == 503
        assert response.json()["detail"] == "Payment provider error: Stripe timeout"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_payment_status_returns_200_when_found(self, client: AsyncClient) -> None:
        """Get-payment-status should return 200 when payment exists."""
        payment_id = "123e4567-e89b-12d3-a456-426614174000"
        mocked_status = {
            "id": payment_id,
            "user_id": str(uuid.uuid4()),
            "amount_cents": 1000,
            "currency": "USD",
            "status": "succeeded",
            "stripe_payment_intent_id": "pi_status_123",
            "error_message": None,
            "created_at": "2026-03-26T10:00:00",
            "updated_at": "2026-03-26T10:05:00",
        }

        with patch.object(
            routes.payment_processor,
            "get_payment_status",
            AsyncMock(return_value=mocked_status),
        ):
            response = await client.get(f"/payments/{payment_id}")

        assert response.status_code == 200
        data = response.json()
        assert data["id"] == payment_id
        assert data["status"] == "succeeded"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_payment_status_returns_404_when_missing(self, client: AsyncClient) -> None:
        """Missing payments should map to HTTP 404."""
        payment_id = "123e4567-e89b-12d3-a456-426614174000"

        with patch.object(
            routes.payment_processor,
            "get_payment_status",
            AsyncMock(return_value=None),
        ):
            response = await client.get(f"/payments/{payment_id}")

        assert response.status_code == 404
        assert response.json()["detail"] == "Payment not found"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_get_payment_status_returns_400_for_invalid_identifier(
        self,
        client: AsyncClient,
    ) -> None:
        """Invalid payment IDs should map to HTTP 400."""
        invalid_payment_id = "not-a-valid-uuid"

        with patch.object(
            routes.payment_processor,
            "get_payment_status",
            AsyncMock(side_effect=PaymentValidationError("payment_id must be a valid UUID")),
        ):
            response = await client.get(f"/payments/{invalid_payment_id}")

        assert response.status_code == 400
        assert response.json()["detail"] == "payment_id must be a valid UUID"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_metrics_endpoint(self, client: AsyncClient) -> None:
        """Prometheus metrics endpoint should expose text output."""
        response = await client.get("/metrics")

        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]