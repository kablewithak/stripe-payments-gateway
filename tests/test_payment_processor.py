"""
Unit tests for payment processor.
"""
from __future__ import annotations

import uuid
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from core.idempotency import IdempotencyManager
from core.payment_processor import PaymentError, PaymentProcessor, PaymentValidationError
from integrations.stripe_client import StripeClient, StripeError, StripeErrorType


class TestPaymentProcessor:
    """Test suite for PaymentProcessor."""

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
                db=test_db,
            )

        assert result["amount_cents"] == 1000
        assert result["currency"] == "USD"
        assert result["status"] == "requires_payment_method"
        assert result["stripe_payment_intent_id"] == "pi_test_123"

        mock_acquire_lock.assert_awaited_once()
        mock_release_lock.assert_awaited_once()

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_stripe_error(self, test_db: Any) -> None:
        """Test payment creation with Stripe error."""
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
        assert len(key.split(":")) == 2  # user_id:request_hash