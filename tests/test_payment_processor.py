"""
Unit tests for payment processor.
"""
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import stripe

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
        # Should not raise any exception

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
    async def test_create_payment_success(self, test_db: Any, mocker: Any) -> None:
        """Test successful payment creation."""
        # Mock Stripe client
        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_payment_intent = MagicMock()
        mock_payment_intent.id = "pi_test_123"
        mock_payment_intent.status = "requires_payment_method"
        mock_stripe_client.create_payment_intent.return_value = mock_payment_intent

        # Mock idempotency manager
        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.return_value = None

        # Mock Redlock
        mock_lock = MagicMock()
        mock_lock.__bool__ = lambda self: True

        processor = PaymentProcessor(
            stripe_client=mock_stripe_client,
            idempotency_manager=mock_idempotency,
        )

        with patch.object(processor, "_get_redlock") as mock_redlock:
            mock_redlock.return_value.lock.return_value = mock_lock
            mock_redlock.return_value.unlock = MagicMock()

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

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_stripe_error(self, test_db: Any, mocker: Any) -> None:
        """Test payment creation with Stripe error."""
        # Mock Stripe client to raise error
        mock_stripe_client = AsyncMock(spec=StripeClient)
        mock_stripe_client.create_payment_intent.side_effect = StripeError(
            "Card declined",
            StripeErrorType.PERMANENT,
        )

        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.return_value = None

        mock_lock = MagicMock()
        mock_lock.__bool__ = lambda self: True

        processor = PaymentProcessor(
            stripe_client=mock_stripe_client,
            idempotency_manager=mock_idempotency,
        )

        with patch.object(processor, "_get_redlock") as mock_redlock:
            mock_redlock.return_value.lock.return_value = mock_lock
            mock_redlock.return_value.unlock = MagicMock()

            with pytest.raises(PaymentError, match="Payment failed"):
                await processor.create_payment(
                    user_id=uuid.uuid4(),
                    amount_cents=1000,
                    currency="USD",
                    db=test_db,
                )

    @pytest.mark.unit
    @pytest.mark.asyncio
    async def test_create_payment_idempotent(self, test_db: Any) -> None:
        """Test idempotent payment creation."""
        cached_response = {
            "id": str(uuid.uuid4()),
            "user_id": str(uuid.uuid4()),
            "amount_cents": 1000,
            "currency": "USD",
            "status": "succeeded",
        }

        mock_idempotency = AsyncMock(spec=IdempotencyManager)
        mock_idempotency.check_idempotency.return_value = cached_response

        processor = PaymentProcessor(idempotency_manager=mock_idempotency)

        result = await processor.create_payment(
            user_id=uuid.uuid4(),
            amount_cents=1000,
            currency="USD",
            db=test_db,
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
        assert len(key.split(":")) == 3  # user_id:payment_hash:timestamp_hash
