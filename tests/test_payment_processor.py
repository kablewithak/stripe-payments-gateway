"""
Unit tests for payment processor.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.idempotency import IdempotencyManager
from core.payment_processor import (
    PaymentConflictError,
    PaymentFailedError,
    PaymentProcessor,
    PaymentProviderError,
    PaymentValidationError,
)
from integrations.stripe_client import StripeClient, StripeError, StripeErrorType


class TestPaymentProcessor:
    """Test suite for PaymentProcessor."""

    @staticmethod
    def _first_non_permanent_error_type() -> StripeErrorType:
        """Return any non-permanent Stripe error type for transient/provider tests."""
        for error_type in StripeErrorType:
            if error_type != StripeErrorType.PERMANENT:
                return error_type

        pytest.skip("StripeErrorType has no non-permanent member to test provider failures")

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_validate_payment_request_valid(self) -> None:
        """Test payment request validation with valid data."""
        PaymentProcessor._validate_payment_request(
            user_id=uuid.uuid4(),
            amount_cents=1000,
            currency="USD",
        )

    @pytest.mark.unit
    def test_validate_payment_request_negative_amount(self) -> None:
        """Test payment request validation with negative amount."""
        with pytest.raises(PaymentValidationError, match="Amount must be positive"):
            PaymentProcessor._validate_payment_request(
                user_id=uuid.uuid4(),
                amount_cents=-100,
                currency="USD",
            )

    @pytest.mark.unit
    def test_validate_payment_request_below_minimum(self) -> None:
        """Test payment request validation below minimum amount."""
        with pytest.raises(PaymentValidationError, match="at least 50 cents"):
            PaymentProcessor._validate_payment_request(
                user_id=uuid.uuid4(),
                amount_cents=25,
                currency="USD",
            )

    @pytest.mark.unit
    def test_validate_payment_request_invalid_currency(self) -> None:
        """Test payment request validation with invalid currency."""
        with pytest.raises(PaymentValidationError, match="Currency must be 3-letter code"):
            PaymentProcessor._validate_payment_request(
                user_id=uuid.uuid4(),
                amount_cents=1000,
                currency="US",
            )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_ensure_redis_respects_injected_client(self) -> None:
        """Injected Redis client should be reused, not silently replaced."""
        injected_redis = AsyncMock()
        processor = PaymentProcessor(redis_client=injected_redis)

        with patch("core.payment_processor.aioredis.from_url", new_callable=AsyncMock) as mock_from_url:
            result = await processor._ensure_redis()

        assert result is injected_redis
        mock_from_url.assert_not_awaited()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_success(self, test_db: Any) -> None:
        """Test successful payment creation."""
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
        mock_idempotency.store_response.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_permanent_stripe_error_raises_payment_failed_error(
        self,
        test_db: Any,
    ) -> None:
        """Permanent Stripe failures should become PaymentFailedError."""
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
            with pytest.raises(PaymentFailedError, match="Payment failed: Card declined"):
                await processor.create_payment(
                    user_id=uuid.uuid4(),
                    amount_cents=1000,
                    currency="USD",
                    db=test_db,
                )

        mock_acquire_lock.assert_awaited_once()
        mock_release_lock.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_transient_stripe_error_raises_payment_provider_error(
        self,
        test_db: Any,
    ) -> None:
        """Non-permanent Stripe failures should become PaymentProviderError."""
        transient_error_type = self._first_non_permanent_error_type()

        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_stripe_client.create_payment_intent.side_effect = StripeError(
            "Stripe timeout",
            transient_error_type,
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
            with pytest.raises(PaymentProviderError, match="Payment provider error: Stripe timeout"):
                await processor.create_payment(
                    user_id=uuid.uuid4(),
                    amount_cents=1000,
                    currency="USD",
                    db=test_db,
                )

        mock_acquire_lock.assert_awaited_once()
        mock_release_lock.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_conflict_raises_payment_conflict_error(
        self,
        test_db: Any,
    ) -> None:
        """Lock conflicts should raise PaymentConflictError."""
        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.side_effect = [None, None]

        mock_redis = AsyncMock()
        mock_redis.set.return_value = False

        processor = PaymentProcessor(
            stripe_client=mock_stripe_client,
            idempotency_manager=mock_idempotency,
            redis_client=mock_redis,
        )

        with pytest.raises(PaymentConflictError, match="Payment already in progress"):
            await processor.create_payment(
                user_id=uuid.uuid4(),
                amount_cents=1000,
                currency="USD",
                db=test_db,
            )

        mock_redis.set.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_succeeds_when_redis_locking_degrades(
        self,
        test_db: Any,
    ) -> None:
        """Redis unavailability should degrade gracefully, not block payment creation."""
        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_payment_intent = MagicMock()
        mock_payment_intent.id = "pi_test_redis_degraded"
        mock_payment_intent.status = "requires_payment_method"
        mock_stripe_client.create_payment_intent.return_value = mock_payment_intent

        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.side_effect = [None, None]

        processor = PaymentProcessor(
            stripe_client=mock_stripe_client,
            idempotency_manager=mock_idempotency,
        )

        with patch.object(
            processor,
            "_ensure_redis",
            AsyncMock(side_effect=RuntimeError("Redis unavailable")),
        ):
            result = await processor.create_payment(
                user_id=uuid.uuid4(),
                amount_cents=1000,
                currency="USD",
                db=test_db,
            )

        assert result["stripe_payment_intent_id"] == "pi_test_redis_degraded"
        assert result["status"] == "requires_payment_method"
        mock_stripe_client.create_payment_intent.assert_awaited_once()
        mock_idempotency.store_response.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_idempotent(self) -> None:
        """Test idempotent payment creation."""
        cached_response = {
            "id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "amount_cents": 1000,
            "currency": "USD",
            "status": "requires_payment_method",
            "stripe_payment_intent_id": "pi_cached",
            "idempotency_key": "cached-key",
            "created_at": "2026-03-24T00:00:00+00:00",
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
    def test_generate_idempotency_key(self) -> None:
        """Test idempotency key generation."""
        user_id = uuid.uuid4()
        key = IdempotencyManager.generate_key(
            user_id=user_id,
            amount_cents=1000,
            currency="USD",
        )

        assert isinstance(key, str)
        assert str(user_id) in key
        assert len(key.split(":")) == 2