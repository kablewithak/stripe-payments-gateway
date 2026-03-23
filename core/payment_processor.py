"""
Main payment processor with distributed locking and idempotency.
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any

import redis.asyncio as aioredis
import structlog
from redlock import Redlock
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from core.idempotency import IdempotencyManager
from database.models import OutboxEvent, Payment, PaymentEvent
from integrations.stripe_client import StripeClient, StripeError, StripeErrorType

logger = structlog.get_logger(__name__)


class PaymentError(Exception):
    """Base exception for payment processing errors."""


class PaymentValidationError(PaymentError):
    """Raised when payment input validation fails."""


class PaymentProcessor:
    """
    Main payment processing orchestrator.

    Handles validation, idempotency, persistence, Stripe interaction,
    audit events, and transactional outbox writes.
    """

    def __init__(
        self,
        stripe_client: StripeClient | None = None,
        idempotency_manager: IdempotencyManager | None = None,
        redis_client: aioredis.Redis | None = None,
    ) -> None:
        self.settings = get_settings()
        self.stripe_client = stripe_client or StripeClient()
        self.idempotency_manager = idempotency_manager or IdempotencyManager()
        self.redis_client = redis_client
        self._redis_initialized = False
        self.redlock: Redlock | None = None

        logger.info("payment_processor_initialized")

    async def _ensure_redis(self) -> aioredis.Redis:
        """Ensure Redis client is initialized."""
        if self.redis_client is None or not self._redis_initialized:
            self.redis_client = await aioredis.from_url(
                self.settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            self._redis_initialized = True
        return self.redis_client

    def _get_redlock(self) -> Redlock:
        """Get or create Redlock instance."""
        if self.redlock is None:
            self.redlock = Redlock([{"url": self.settings.redis_url}])
        return self.redlock

    @staticmethod
    def _validate_payment_request(
        user_id: str | uuid.UUID,
        amount_cents: int,
        currency: str,
    ) -> None:
        """Validate payment request parameters."""
        if not user_id:
            raise PaymentValidationError("User ID is required")

        if amount_cents <= 0:
            raise PaymentValidationError("Amount must be positive")

        if amount_cents < 50:
            raise PaymentValidationError("Amount must be at least 50 cents")

        if len(currency) != 3:
            raise PaymentValidationError("Currency must be 3-letter code")

    @staticmethod
    def _build_payment_response(payment: Payment) -> dict[str, Any]:
        """Build API response from a payment model."""
        return {
            "id": str(payment.id),
            "user_id": str(payment.user_id),
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "status": payment.status,
            "stripe_payment_intent_id": payment.stripe_payment_intent_id,
            "idempotency_key": payment.idempotency_key,
            "created_at": payment.created_at.isoformat(),
        }

    async def _record_payment_event(
        self,
        db: AsyncSession,
        payment_id: uuid.UUID,
        event_type: str,
        event_data: dict[str, Any],
        correlation_id: uuid.UUID,
    ) -> None:
        """Record an immutable payment event."""
        event = PaymentEvent(
            payment_id=payment_id,
            event_type=event_type,
            event_data=event_data,
            correlation_id=correlation_id,
            created_at=datetime.utcnow(),
        )
        db.add(event)

    async def _write_outbox_event(
        self,
        db: AsyncSession,
        aggregate_id: uuid.UUID,
        aggregate_type: str,
        event_type: str,
        payload: dict[str, Any],
    ) -> None:
        """Write a transactional outbox event."""
        outbox_event = OutboxEvent(
            aggregate_id=aggregate_id,
            aggregate_type=aggregate_type,
            event_type=event_type,
            payload=payload,
            published=False,
            created_at=datetime.utcnow(),
        )
        db.add(outbox_event)

    async def create_payment(
        self,
        user_id: str | uuid.UUID,
        amount_cents: int,
        currency: str,
        metadata: dict[str, Any] | None = None,
        db: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """
        Create a payment with full idempotency and locking.
        """
        if db is None:
            raise PaymentError("Database session is required")

        correlation_id = uuid.uuid4()
        user_id_uuid = uuid.UUID(str(user_id))
        normalized_currency = currency.upper()

        logger.info(
            "payment_creation_started",
            correlation_id=str(correlation_id),
            user_id=str(user_id_uuid),
            amount_cents=amount_cents,
            currency=normalized_currency,
        )

        self._validate_payment_request(user_id_uuid, amount_cents, normalized_currency)

        idempotency_key = IdempotencyManager.generate_key(
            user_id=user_id_uuid,
            amount_cents=amount_cents,
            currency=normalized_currency,
            metadata=metadata,
        )

        cached_response = await self.idempotency_manager.check_idempotency(idempotency_key, db)
        if cached_response is not None:
            logger.info(
                "payment_idempotent_return",
                correlation_id=str(correlation_id),
                idempotency_key=idempotency_key,
            )
            return cached_response

        lock_key = f"payment:lock:{idempotency_key}"
        redlock = self._get_redlock()
        lock = redlock.lock(lock_key, self.settings.redis_lock_timeout * 1000)

        if not lock:
            logger.warning(
                "payment_lock_acquisition_failed",
                correlation_id=str(correlation_id),
                lock_key=lock_key,
            )
            raise PaymentError("Failed to acquire lock - payment already in progress")

        try:
            cached_response = await self.idempotency_manager.check_idempotency(idempotency_key, db)
            if cached_response is not None:
                logger.info(
                    "payment_idempotent_return_after_lock",
                    correlation_id=str(correlation_id),
                    idempotency_key=idempotency_key,
                )
                return cached_response

            payment_id = uuid.uuid4()
            payment = Payment(
                id=payment_id,
                idempotency_key=idempotency_key,
                user_id=user_id_uuid,
                amount_cents=amount_cents,
                currency=normalized_currency,
                status="pending",
                metadata_json=metadata,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(payment)
            await db.flush()

            await self._record_payment_event(
                db=db,
                payment_id=payment_id,
                event_type="payment.created",
                event_data={
                    "amount_cents": amount_cents,
                    "currency": normalized_currency,
                    "status": "pending",
                },
                correlation_id=correlation_id,
            )

            payment.status = "processing"
            await db.flush()

            await self._record_payment_event(
                db=db,
                payment_id=payment_id,
                event_type="payment.processing",
                event_data={"status": "processing"},
                correlation_id=correlation_id,
            )

            try:
                stripe_metadata: dict[str, Any] = {
                    "payment_id": str(payment_id),
                    "user_id": str(user_id_uuid),
                    "correlation_id": str(correlation_id),
                }
                if metadata:
                    stripe_metadata.update(metadata)

                payment_intent = await self.stripe_client.create_payment_intent(
                    amount_cents=amount_cents,
                    currency=normalized_currency,
                    idempotency_key=idempotency_key,
                    metadata=stripe_metadata,
                )

                payment.stripe_payment_intent_id = payment_intent.id
                payment.status = payment_intent.status
                payment.response_snapshot = {
                    "payment_intent_id": payment_intent.id,
                    "status": payment_intent.status,
                }
                await db.flush()

                await self._record_payment_event(
                    db=db,
                    payment_id=payment_id,
                    event_type="stripe.payment_intent_created",
                    event_data={
                        "payment_intent_id": payment_intent.id,
                        "status": payment_intent.status,
                    },
                    correlation_id=correlation_id,
                )

            except StripeError as exc:
                error_message = str(exc)
                payment.status = "failed"
                payment.error_message = error_message
                payment.response_snapshot = {
                    "error": error_message,
                    "error_type": exc.error_type.value,
                }
                await db.flush()

                await self._record_payment_event(
                    db=db,
                    payment_id=payment_id,
                    event_type="payment.failed",
                    event_data={
                        "error": error_message,
                        "error_type": exc.error_type.value,
                    },
                    correlation_id=correlation_id,
                )

                if exc.error_type == StripeErrorType.PERMANENT:
                    await db.commit()
                    raise PaymentError(f"Payment failed: {error_message}") from exc

                raise

            await self._write_outbox_event(
                db=db,
                aggregate_id=payment_id,
                aggregate_type="payment",
                event_type="payment.created",
                payload={
                    "payment_id": str(payment_id),
                    "user_id": str(user_id_uuid),
                    "amount_cents": amount_cents,
                    "currency": normalized_currency,
                    "status": payment.status,
                    "stripe_payment_intent_id": payment.stripe_payment_intent_id,
                    "created_at": payment.created_at.isoformat(),
                },
            )

            await db.commit()
            await db.refresh(payment)

            response = self._build_payment_response(payment)
            await self.idempotency_manager.store_response(idempotency_key, response)

            logger.info(
                "payment_created_successfully",
                correlation_id=str(correlation_id),
                payment_id=str(payment_id),
                status=payment.status,
            )
            return response

        finally:
            try:
                redlock.unlock(lock)
            except Exception as exc:
                logger.warning(
                    "payment_lock_release_failed",
                    error=str(exc),
                    lock_key=lock_key,
                )
            logger.info(
                "payment_lock_released",
                correlation_id=str(correlation_id),
                lock_key=lock_key,
            )

    async def get_payment_status(
        self,
        payment_id: str | uuid.UUID,
        db: AsyncSession,
    ) -> dict[str, Any] | None:
        """Get payment status by ID."""
        payment_id_uuid = uuid.UUID(str(payment_id))

        stmt = select(Payment).where(Payment.id == payment_id_uuid)
        result = await db.execute(stmt)
        payment = result.scalar_one_or_none()

        if payment is None:
            return None

        return {
            "id": str(payment.id),
            "user_id": str(payment.user_id),
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "status": payment.status,
            "stripe_payment_intent_id": payment.stripe_payment_intent_id,
            "error_message": payment.error_message,
            "created_at": payment.created_at.isoformat(),
            "updated_at": payment.updated_at.isoformat(),
        }

    async def refund_payment(
        self,
        payment_id: str | uuid.UUID,
        amount_cents: int | None = None,
        reason: str | None = None,
        db: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """Refund a payment."""
        if db is None:
            raise PaymentError("Database session is required")

        payment_id_uuid = uuid.UUID(str(payment_id))
        correlation_id = uuid.uuid4()

        stmt = select(Payment).where(Payment.id == payment_id_uuid)
        result = await db.execute(stmt)
        payment = result.scalar_one_or_none()

        if payment is None:
            raise PaymentError(f"Payment {payment_id} not found")

        if payment.status != "succeeded":
            raise PaymentError(f"Cannot refund payment with status: {payment.status}")

        if not payment.stripe_payment_intent_id:
            raise PaymentError("Payment has no Stripe PaymentIntent ID")

        try:
            refund = await self.stripe_client.create_refund(
                payment_intent_id=payment.stripe_payment_intent_id,
                amount_cents=amount_cents,
                reason=reason,
                idempotency_key=f"refund:{payment_id}:{correlation_id}",
            )

            payment.status = "refunded"
            await db.flush()

            await self._record_payment_event(
                db=db,
                payment_id=payment_id_uuid,
                event_type="payment.refunded",
                event_data={
                    "refund_id": refund.id,
                    "amount_cents": amount_cents or payment.amount_cents,
                    "reason": reason,
                },
                correlation_id=correlation_id,
            )

            await db.commit()

            return {
                "payment_id": str(payment_id),
                "refund_id": refund.id,
                "status": refund.status,
                "amount_cents": refund.amount,
            }

        except StripeError as exc:
            logger.error(
                "refund_failed",
                correlation_id=str(correlation_id),
                payment_id=str(payment_id),
                error=str(exc),
            )
            raise PaymentError(f"Refund failed: {exc}") from exc