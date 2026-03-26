"""
Unit tests for Stripe webhook handler.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from database.models import Payment, PaymentEvent
from integrations.webhook_handler import (
    WebhookHandler,
    WebhookProcessingError,
    WebhookVerificationError,
)


class TestWebhookHandler:
    """Test suite for WebhookHandler."""

    @staticmethod
    def _build_event(
        event_id: str,
        event_type: str,
        event_object: dict[str, Any],
    ) -> SimpleNamespace:
        """Build a lightweight fake Stripe event."""
        return SimpleNamespace(
            id=event_id,
            type=event_type,
            data=SimpleNamespace(object=event_object),
        )

    @staticmethod
    async def _create_payment(
        db: AsyncSession,
        *,
        stripe_payment_intent_id: str,
        status: str = "processing",
        error_message: str | None = None,
    ) -> Payment:
        """Create and persist a payment row for webhook tests."""
        now = datetime.utcnow()
        payment = Payment(
            id=uuid.uuid4(),
            idempotency_key=f"idem:{uuid.uuid4()}",
            user_id=uuid.uuid4(),
            amount_cents=1000,
            currency="USD",
            status=status,
            metadata_json={"order_id": "webhook_test_order"},
            stripe_payment_intent_id=stripe_payment_intent_id,
            error_message=error_message,
            created_at=now,
            updated_at=now,
        )
        db.add(payment)
        await db.commit()
        await db.refresh(payment)
        return payment

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ensure_redis_respects_injected_client(self) -> None:
        """Injected Redis client should be reused, not replaced."""
        injected_redis = AsyncMock()
        handler = WebhookHandler(redis_client=injected_redis)

        with patch("integrations.webhook_handler.aioredis.from_url", new_callable=AsyncMock) as mock_from_url:
            result = await handler._ensure_redis()

        assert result is injected_redis
        mock_from_url.assert_not_awaited()

    @pytest.mark.unit
    def test_verify_signature_success(self) -> None:
        """Valid signatures should return the parsed Stripe event."""
        handler = WebhookHandler()
        mock_event = MagicMock()
        mock_event.id = "evt_test_123"
        mock_event.type = "payment_intent.succeeded"

        with patch(
            "integrations.webhook_handler.stripe.Webhook.construct_event",
            return_value=mock_event,
        ) as mock_construct:
            result = handler.verify_signature(
                payload=b'{"id":"evt_test_123"}',
                signature="valid_signature",
                secret="whsec_test",
            )

        assert result is mock_event
        mock_construct.assert_called_once()

    @pytest.mark.unit
    def test_verify_signature_invalid_signature_raises_webhook_verification_error(self) -> None:
        """Invalid webhook signatures should map to WebhookVerificationError."""
        handler = WebhookHandler()

        with patch(
            "integrations.webhook_handler.stripe.Webhook.construct_event",
            side_effect=stripe.error.SignatureVerificationError(
                "Bad signature",
                "invalid_signature",
            ),
        ):
            with pytest.raises(WebhookVerificationError, match="Invalid webhook signature"):
                handler.verify_signature(
                    payload=b"{}",
                    signature="invalid_signature",
                    secret="whsec_test",
                )

    @pytest.mark.unit
    def test_verify_signature_unexpected_error_raises_webhook_verification_error(self) -> None:
        """Unexpected verification errors should map to WebhookVerificationError."""
        handler = WebhookHandler()

        with patch(
            "integrations.webhook_handler.stripe.Webhook.construct_event",
            side_effect=RuntimeError("stripe parsing blew up"),
        ):
            with pytest.raises(WebhookVerificationError, match="Webhook verification failed"):
                handler.verify_signature(
                    payload=b"{}",
                    signature="sig",
                    secret="whsec_test",
                )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_is_event_processed_returns_false_when_redis_unavailable(self) -> None:
        """Dedup check should degrade gracefully when Redis is unavailable."""
        handler = WebhookHandler()

        with patch.object(
            handler,
            "_ensure_redis",
            AsyncMock(side_effect=RuntimeError("Redis unavailable")),
        ):
            result = await handler.is_event_processed("evt_123")

        assert result is False

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_process_event_returns_duplicate_when_already_processed(self) -> None:
        """Already-processed events should return duplicate status."""
        handler = WebhookHandler()
        event = self._build_event(
            "evt_duplicate",
            "payment_intent.succeeded",
            {"id": "pi_123"},
        )

        with patch.object(handler, "is_event_processed", AsyncMock(return_value=True)):
            result = await handler.process_event(event)

        assert result["status"] == "duplicate"
        assert result["event_id"] == "evt_duplicate"
        assert result["message"] == "Event already processed"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_process_event_returns_no_handler_and_marks_processed(self) -> None:
        """Unknown event types should return no_handler and still mark processed."""
        handler = WebhookHandler()
        event = self._build_event(
            "evt_no_handler",
            "customer.created",
            {"id": "cus_123"},
        )

        mock_mark_processed = AsyncMock()

        with patch.object(handler, "is_event_processed", AsyncMock(return_value=False)), patch.object(
            handler,
            "mark_event_processed",
            mock_mark_processed,
        ):
            result = await handler.process_event(event)

        assert result["status"] == "no_handler"
        assert result["event_id"] == "evt_no_handler"
        mock_mark_processed.assert_awaited_once_with("evt_no_handler")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_process_event_success_calls_handler_and_marks_processed(self) -> None:
        """Handled events should call the registered handler and be marked processed."""
        handler = WebhookHandler()
        event_object = {"id": "pi_123", "amount": 1000}
        event = self._build_event(
            "evt_success",
            "payment_intent.succeeded",
            event_object,
        )

        mock_handler = AsyncMock(return_value={"status": "succeeded"})
        handler.register_handler("payment_intent.succeeded", mock_handler)

        mock_mark_processed = AsyncMock()

        with patch.object(handler, "is_event_processed", AsyncMock(return_value=False)), patch.object(
            handler,
            "mark_event_processed",
            mock_mark_processed,
        ):
            result = await handler.process_event(event)

        assert result["status"] == "success"
        assert result["event_id"] == "evt_success"
        assert result["result"] == {"status": "succeeded"}
        mock_handler.assert_awaited_once_with(event_object)
        mock_mark_processed.assert_awaited_once_with("evt_success")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_process_event_success_passes_db_when_provided(self, test_db: AsyncSession) -> None:
        """When a DB session is provided, process_event should pass it to the handler."""
        handler = WebhookHandler()
        event_object = {"id": "pi_with_db"}
        event = self._build_event(
            "evt_with_db",
            "payment_intent.succeeded",
            event_object,
        )

        mock_handler = AsyncMock(return_value={"status": "ok"})
        handler.register_handler("payment_intent.succeeded", mock_handler)

        with patch.object(handler, "is_event_processed", AsyncMock(return_value=False)), patch.object(
            handler,
            "mark_event_processed",
            AsyncMock(),
        ):
            result = await handler.process_event(event, db=test_db)

        assert result["status"] == "success"
        mock_handler.assert_awaited_once_with(event_object, test_db)

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_process_event_wraps_handler_failure(self) -> None:
        """Handler exceptions should be wrapped in WebhookProcessingError."""
        handler = WebhookHandler()
        event = self._build_event(
            "evt_failure",
            "payment_intent.succeeded",
            {"id": "pi_123"},
        )

        failing_handler = AsyncMock(side_effect=RuntimeError("boom"))
        handler.register_handler("payment_intent.succeeded", failing_handler)

        mock_mark_processed = AsyncMock()

        with patch.object(handler, "is_event_processed", AsyncMock(return_value=False)), patch.object(
            handler,
            "mark_event_processed",
            mock_mark_processed,
        ):
            with pytest.raises(
                WebhookProcessingError,
                match="Failed to process event evt_failure: boom",
            ):
                await handler.process_event(event)

        mock_mark_processed.assert_not_awaited()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_payment_intent_succeeded_updates_payment_and_records_event(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Succeeded webhook should update payment snapshot and write an audit event."""
        handler = WebhookHandler()
        payment_intent_id = "pi_success_123"
        payment = await self._create_payment(
            test_db,
            stripe_payment_intent_id=payment_intent_id,
            status="processing",
        )

        result = await handler.handle_payment_intent_succeeded(
            {
                "id": payment_intent_id,
                "amount": 1000,
                "currency": "usd",
            },
            test_db,
        )

        refreshed_payment = await test_db.get(Payment, payment.id)
        assert refreshed_payment is not None
        assert refreshed_payment.status == "succeeded"
        assert result["status"] == "succeeded"
        assert result["payment_intent_id"] == payment_intent_id
        assert result["payment_id"] == str(payment.id)
        assert result["audit_log"] is True

        event_query = select(PaymentEvent).where(
            PaymentEvent.payment_id == payment.id,
            PaymentEvent.event_type == "payment_intent.succeeded",
        )
        event_result = await test_db.execute(event_query)
        events = event_result.scalars().all()

        assert len(events) == 1
        assert events[0].event_data["id"] == payment_intent_id

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_payment_intent_succeeded_returns_not_found_when_payment_missing(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Missing payments should not crash success webhook handling."""
        handler = WebhookHandler()

        result = await handler.handle_payment_intent_succeeded(
            {
                "id": "pi_missing_success",
                "amount": 1000,
                "currency": "usd",
            },
            test_db,
        )

        assert result["status"] == "not_found"
        assert result["payment_intent_id"] == "pi_missing_success"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_payment_intent_payment_failed_updates_payment_and_records_event(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Failed webhook should update payment snapshot and write an audit event."""
        handler = WebhookHandler()
        payment_intent_id = "pi_failed_123"
        payment = await self._create_payment(
            test_db,
            stripe_payment_intent_id=payment_intent_id,
            status="processing",
        )

        result = await handler.handle_payment_intent_payment_failed(
            {
                "id": payment_intent_id,
                "last_payment_error": {"message": "Card declined"},
            },
            test_db,
        )

        refreshed_payment = await test_db.get(Payment, payment.id)
        assert refreshed_payment is not None
        assert refreshed_payment.status == "failed"
        assert refreshed_payment.error_message == "Card declined"
        assert result["status"] == "failed"
        assert result["payment_intent_id"] == payment_intent_id
        assert result["payment_id"] == str(payment.id)
        assert result["audit_log"] is True

        event_query = select(PaymentEvent).where(
            PaymentEvent.payment_id == payment.id,
            PaymentEvent.event_type == "payment_intent.payment_failed",
        )
        event_result = await test_db.execute(event_query)
        events = event_result.scalars().all()

        assert len(events) == 1
        assert events[0].event_data["id"] == payment_intent_id

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_payment_intent_payment_failed_returns_not_found_when_payment_missing(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Missing payments should not crash failure webhook handling."""
        handler = WebhookHandler()

        result = await handler.handle_payment_intent_payment_failed(
            {
                "id": "pi_missing_failure",
                "last_payment_error": {"message": "Card declined"},
            },
            test_db,
        )

        assert result["status"] == "not_found"
        assert result["payment_intent_id"] == "pi_missing_failure"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_charge_refunded_updates_payment_and_records_event(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Refunded webhook should update payment snapshot and write an audit event."""
        handler = WebhookHandler()
        payment_intent_id = "pi_refunded_123"
        payment = await self._create_payment(
            test_db,
            stripe_payment_intent_id=payment_intent_id,
            status="succeeded",
        )

        result = await handler.handle_charge_refunded(
            {
                "id": "ch_123",
                "payment_intent": payment_intent_id,
                "amount_refunded": 1000,
            },
            test_db,
        )

        refreshed_payment = await test_db.get(Payment, payment.id)
        assert refreshed_payment is not None
        assert refreshed_payment.status == "refunded"
        assert result["status"] == "refunded"
        assert result["payment_intent_id"] == payment_intent_id
        assert result["payment_id"] == str(payment.id)
        assert result["audit_log"] is True

        event_query = select(PaymentEvent).where(
            PaymentEvent.payment_id == payment.id,
            PaymentEvent.event_type == "charge.refunded",
        )
        event_result = await test_db.execute(event_query)
        events = event_result.scalars().all()

        assert len(events) == 1
        assert events[0].event_data["id"] == "ch_123"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_charge_refunded_returns_skipped_without_payment_intent(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Refund webhooks without payment_intent should be skipped cleanly."""
        handler = WebhookHandler()

        result = await handler.handle_charge_refunded(
            {"id": "ch_missing_pi"},
            test_db,
        )

        assert result["status"] == "skipped"
        assert result["reason"] == "No payment_intent associated"

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_handle_charge_refunded_returns_not_found_when_payment_missing(
        self,
        test_db: AsyncSession,
    ) -> None:
        """Missing payments should not crash refund webhook handling."""
        handler = WebhookHandler()

        result = await handler.handle_charge_refunded(
            {
                "id": "ch_missing_payment",
                "payment_intent": "pi_missing_refund",
            },
            test_db,
        )

        assert result["status"] == "not_found"
        assert result["payment_intent_id"] == "pi_missing_refund"