"""
Integration tests for refund and webhook route contracts.
"""
from __future__ import annotations

import uuid
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from api import routes
from core.payment_processor import (
    PaymentNotFoundError,
    PaymentProviderError,
    PaymentValidationError,
    RefundNotAllowedError,
)
from integrations.webhook_handler import WebhookVerificationError


class TestRefundAndWebhookRoutes:
    """Integration tests for refund and webhook API routes."""

    @staticmethod
    def _build_refund_response(payment_id: str) -> dict:
        """Build a valid refund response payload."""
        return {
            "payment_id": payment_id,
            "refund_id": "re_test_123",
            "status": "succeeded",
            "amount_cents": 500,
        }

    @staticmethod
    def _build_webhook_event(
        event_id: str,
        event_type: str,
        event_object: dict,
    ) -> SimpleNamespace:
        """Build a lightweight fake Stripe event."""
        return SimpleNamespace(
            id=event_id,
            type=event_type,
            data=SimpleNamespace(object=event_object),
        )

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_refund_payment_returns_200_on_success(self, client: AsyncClient) -> None:
        """Refund endpoint should return 200 with the expected schema."""
        payment_id = str(uuid.uuid4())
        mocked_refund = self._build_refund_response(payment_id)

        with patch.object(
            routes.payment_processor,
            "refund_payment",
            AsyncMock(return_value=mocked_refund),
        ) as mock_refund_payment:
            response = await client.post(
                f"/payments/{payment_id}/refund",
                json={"amount_cents": 500, "reason": "requested_by_customer"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["payment_id"] == payment_id
        assert data["refund_id"] == "re_test_123"
        assert data["status"] == "succeeded"
        assert data["amount_cents"] == 500
        mock_refund_payment.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_refund_payment_returns_400_on_validation_error(
        self,
        client: AsyncClient,
    ) -> None:
        """Refund endpoint should map validation failures to HTTP 400."""
        payment_id = str(uuid.uuid4())

        with patch.object(
            routes.payment_processor,
            "refund_payment",
            AsyncMock(side_effect=PaymentValidationError("payment_id must be a valid UUID")),
        ):
            response = await client.post(
                f"/payments/{payment_id}/refund",
                json={"reason": "requested_by_customer"},
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "payment_id must be a valid UUID"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_refund_payment_returns_404_when_payment_missing(
        self,
        client: AsyncClient,
    ) -> None:
        """Refund endpoint should map missing payments to HTTP 404."""
        payment_id = str(uuid.uuid4())

        with patch.object(
            routes.payment_processor,
            "refund_payment",
            AsyncMock(side_effect=PaymentNotFoundError(f"Payment {payment_id} not found")),
        ):
            response = await client.post(
                f"/payments/{payment_id}/refund",
                json={"reason": "duplicate"},
            )

        assert response.status_code == 404
        assert response.json()["detail"] == f"Payment {payment_id} not found"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_refund_payment_returns_409_when_refund_not_allowed(
        self,
        client: AsyncClient,
    ) -> None:
        """Refund endpoint should map invalid payment state to HTTP 409."""
        payment_id = str(uuid.uuid4())

        with patch.object(
            routes.payment_processor,
            "refund_payment",
            AsyncMock(side_effect=RefundNotAllowedError("Cannot refund payment with status: pending")),
        ):
            response = await client.post(
                f"/payments/{payment_id}/refund",
                json={"reason": "requested_by_customer"},
            )

        assert response.status_code == 409
        assert response.json()["detail"] == "Cannot refund payment with status: pending"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_refund_payment_returns_503_on_provider_error(
        self,
        client: AsyncClient,
    ) -> None:
        """Refund endpoint should map upstream/provider failures to HTTP 503."""
        payment_id = str(uuid.uuid4())

        with patch.object(
            routes.payment_processor,
            "refund_payment",
            AsyncMock(side_effect=PaymentProviderError("Refund failed: Stripe timeout")),
        ):
            response = await client.post(
                f"/payments/{payment_id}/refund",
                json={"reason": "requested_by_customer"},
            )

        assert response.status_code == 503
        assert response.json()["detail"] == "Refund failed: Stripe timeout"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_webhook_returns_200_on_success(self, client: AsyncClient) -> None:
        """Webhook route should return 200 with the expected schema on success."""
        event = self._build_webhook_event(
            event_id="evt_success_123",
            event_type="payment_intent.succeeded",
            event_object={"id": "pi_123"},
        )

        with patch.object(
            routes.webhook_handler,
            "verify_signature",
            return_value=event,
        ) as mock_verify_signature, patch.object(
            routes.webhook_handler,
            "process_event",
            AsyncMock(
                return_value={
                    "status": "success",
                    "event_id": "evt_success_123",
                    "message": "processed",
                }
            ),
        ) as mock_process_event:
            response = await client.post(
                "/webhooks/stripe",
                content=b'{"id":"evt_success_123"}',
                headers={"Stripe-Signature": "sig_test"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "success"
        assert data["event_id"] == "evt_success_123"
        assert data["message"] == "processed"
        mock_verify_signature.assert_called_once()
        mock_process_event.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_webhook_returns_400_on_invalid_signature(self, client: AsyncClient) -> None:
        """Webhook route should map invalid signatures to HTTP 400."""
        with patch.object(
            routes.webhook_handler,
            "verify_signature",
            side_effect=WebhookVerificationError("Invalid webhook signature: bad signature"),
        ):
            response = await client.post(
                "/webhooks/stripe",
                content=b"{}",
                headers={"Stripe-Signature": "bad_sig"},
            )

        assert response.status_code == 400
        assert response.json()["detail"] == "Invalid webhook signature: bad signature"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_webhook_returns_200_for_duplicate_event(self, client: AsyncClient) -> None:
        """Duplicate webhook events should return 200 with duplicate status."""
        event = self._build_webhook_event(
            event_id="evt_duplicate_123",
            event_type="payment_intent.succeeded",
            event_object={"id": "pi_123"},
        )

        with patch.object(
            routes.webhook_handler,
            "verify_signature",
            return_value=event,
        ), patch.object(
            routes.webhook_handler,
            "process_event",
            AsyncMock(
                return_value={
                    "status": "duplicate",
                    "event_id": "evt_duplicate_123",
                    "message": "Event already processed",
                }
            ),
        ):
            response = await client.post(
                "/webhooks/stripe",
                content=b'{"id":"evt_duplicate_123"}',
                headers={"Stripe-Signature": "sig_test"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "duplicate"
        assert data["event_id"] == "evt_duplicate_123"
        assert data["message"] == "Event already processed"

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_webhook_returns_200_for_unhandled_event_type(self, client: AsyncClient) -> None:
        """Unhandled webhook event types should still return 200 with no_handler status."""
        event = self._build_webhook_event(
            event_id="evt_no_handler_123",
            event_type="customer.created",
            event_object={"id": "cus_123"},
        )

        with patch.object(
            routes.webhook_handler,
            "verify_signature",
            return_value=event,
        ), patch.object(
            routes.webhook_handler,
            "process_event",
            AsyncMock(
                return_value={
                    "status": "no_handler",
                    "event_id": "evt_no_handler_123",
                    "message": "No handler registered for event type: customer.created",
                }
            ),
        ):
            response = await client.post(
                "/webhooks/stripe",
                content=b'{"id":"evt_no_handler_123"}',
                headers={"Stripe-Signature": "sig_test"},
            )

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "no_handler"
        assert data["event_id"] == "evt_no_handler_123"
        assert data["message"] == "No handler registered for event type: customer.created"