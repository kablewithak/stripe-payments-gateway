from __future__ import annotations

import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.idempotency import IdempotencyManager
from core.payment_processor import PaymentError, PaymentProcessor, PaymentValidationError
from integrations.stripe_client import StripeClient, StripeError, StripeErrorType


class TestDay1Core:
    @pytest.mark.unit
    def test_generate_key_is_deterministic_for_same_request(self) -> None:
        user_id = uuid.uuid4()
        metadata = {"order_id": "123", "product": "premium"}

        key_one = IdempotencyManager.generate_key(
            user_id=user_id,
            amount_cents=1000,
            currency="usd",
            metadata=metadata,
        )
        key_two = IdempotencyManager.generate_key(
            user_id=user_id,
            amount_cents=1000,
            currency="USD",
            metadata={"product": "premium", "order_id": "123"},
        )

        assert key_one == key_two

    @pytest.mark.unit
    def test_generate_key_changes_when_request_changes(self) -> None:
        user_id = uuid.uuid4()

        key_one = IdempotencyManager.generate_key(
            user_id=user_id,
            amount_cents=1000,
            currency="USD",
            metadata={"order_id": "123"},
        )
        key_two = IdempotencyManager.generate_key(
            user_id=user_id,
            amount_cents=2000,
            currency="USD",
            metadata={"order_id": "123"},
        )

        assert key_one != key_two

    @pytest.mark.unit
    def test_validate_payment_request_rejects_small_amount(self) -> None:
        with pytest.raises(PaymentValidationError, match="at least 50 cents"):
            PaymentProcessor._validate_payment_request(
                user_id=uuid.uuid4(),
                amount_cents=25,
                currency="USD",
            )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_returns_cached_response_when_idempotent(self) -> None:
        cached_response = {
            "id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "amount_cents": 1000,
            "currency": "USD",
            "status": "requires_payment_method",
            "stripe_payment_intent_id": "pi_cached",
            "idempotency_key": "cached-key",
            "created_at": "2026-03-23T10:00:00+00:00",
        }

        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.return_value = cached_response

        processor = PaymentProcessor(idempotency_manager=mock_idempotency)
        fake_db = MagicMock()

        result = await processor.create_payment(
            user_id=uuid.uuid4(),
            amount_cents=1000,
            currency="USD",
            db=fake_db,
        )

        assert result == cached_response

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_success_persists_and_returns_schema_shape(self, test_db) -> None:
        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_payment_intent = MagicMock()
        mock_payment_intent.id = "pi_test_123"
        mock_payment_intent.status = "requires_payment_method"
        mock_stripe_client.create_payment_intent.return_value = mock_payment_intent

        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.side_effect = [None, None]

        processor = PaymentProcessor(
            stripe_client=mock_stripe_client,
            idempotency_manager=mock_idempotency,
        )

        mock_acquire_lock = AsyncMock(return_value="lock-token")
        mock_release_lock = AsyncMock()

        with patch.object(processor, "_acquire_payment_lock", mock_acquire_lock), patch.object(
            processor,
            "_release_payment_lock",
            mock_release_lock,
        ):
            result = await processor.create_payment(
                user_id=uuid.uuid4(),
                amount_cents=1000,
                currency="USD",
                metadata={"order_id": "order_123"},
                db=test_db,
            )

        assert result["amount_cents"] == 1000
        assert result["currency"] == "USD"
        assert result["status"] == "requires_payment_method"
        assert result["stripe_payment_intent_id"] == "pi_test_123"
        assert "idempotency_key" in result
        assert "user_id" in result
        assert "created_at" in result

        mock_acquire_lock.assert_awaited_once()
        mock_release_lock.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_permanent_stripe_error_raises_payment_error(self, test_db) -> None:
        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_stripe_client.create_payment_intent.side_effect = StripeError(
            "Card declined",
            StripeErrorType.PERMANENT,
        )

        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.side_effect = [None, None]

        processor = PaymentProcessor(
            stripe_client=mock_stripe_client,
            idempotency_manager=mock_idempotency,
        )

        mock_acquire_lock = AsyncMock(return_value="lock-token")
        mock_release_lock = AsyncMock()

        with patch.object(processor, "_acquire_payment_lock", mock_acquire_lock), patch.object(
            processor,
            "_release_payment_lock",
            mock_release_lock,
        ):
            with pytest.raises(PaymentError, match="Payment failed"):
                await processor.create_payment(
                    user_id=uuid.uuid4(),
                    amount_cents=1000,
                    currency="USD",
                    db=test_db,
                )

        mock_acquire_lock.assert_awaited_once()
        mock_release_lock.assert_awaited_once()