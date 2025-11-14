"""
Main payment processor with distributed locking and idempotency.

Orchestrates the complete payment flow:
1. Validate input
2. Check idempotency
3. Acquire distributed lock
4. Create payment record
5. Call Stripe API
6. Write to outbox
7. Commit transaction
8. Release lock
"""
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

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

    pass


class PaymentValidationError(PaymentError):
    """Raised when payment input validation fails."""

    pass


class PaymentProcessor:
    """
    Main payment processing orchestrator.

    Handles the complete payment lifecycle with proper error handling,
    idempotency, and distributed locking.
    """

    def __init__(
        self,
        stripe_client: Optional[StripeClient] = None,
        idempotency_manager: Optional[IdempotencyManager] = None,
        redis_client: Optional[aioredis.Redis] = None,
    ):
        """
        Initialize payment processor.

        Args:
            stripe_client: Optional Stripe client
            idempotency_manager: Optional idempotency manager
            redis_client: Optional Redis client for distributed locking
        """
        self.settings = get_settings()
        self.stripe_client = stripe_client or StripeClient()
        self.idempotency_manager = idempotency_manager or IdempotencyManager()
        self.redis_client = redis_client
        self._redis_initialized = False

        # Initialize Redlock for distributed locking
        self.redlock: Optional[Redlock] = None

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
            # Parse Redis URL for Redlock
            redis_url = self.settings.redis_url
            self.redlock = Redlock([{"url": redis_url}])
        return self.redlock

    @staticmethod
    def _validate_payment_request(
        user_id: str | uuid.UUID,
        amount_cents: int,
        currency: str,
    ) -> None:
        """
        Validate payment request parameters.

        Args:
            user_id: User identifier
            amount_cents: Payment amount in cents
            currency: Currency code

        Raises:
            PaymentValidationError: If validation fails
        """
        if amount_cents <= 0:
            raise PaymentValidationError("Amount must be positive")

        if amount_cents < 50:  # Stripe minimum
            raise PaymentValidationError("Amount must be at least 50 cents")

        if len(currency) != 3:
            raise PaymentValidationError("Currency must be 3-letter code")

        if not user_id:
            raise PaymentValidationError("User ID is required")

    async def _record_payment_event(
        self,
        db: AsyncSession,
        payment_id: uuid.UUID,
        event_type: str,
        event_data: Dict[str, Any],
        correlation_id: uuid.UUID,
    ) -> None:
        """
        Record a payment event for audit trail.

        Args:
            db: Database session
            payment_id: Payment ID
            event_type: Event type
            event_data: Event data
            correlation_id: Correlation ID for tracing
        """
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
        payload: Dict[str, Any],
    ) -> None:
        """
        Write event to transactional outbox.

        Args:
            db: Database session
            aggregate_id: Aggregate ID (e.g., payment ID)
            aggregate_type: Aggregate type (e.g., 'payment')
            event_type: Event type (e.g., 'payment.created')
            payload: Event payload
        """
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
        metadata: Optional[Dict[str, Any]] = None,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """
        Create a payment with full idempotency and locking.

        Flow:
        1. Validate input
        2. Generate idempotency key
        3. Check idempotency cache
        4. Acquire distributed lock
        5. Begin database transaction
        6. Create payment record
        7. Call Stripe API
        8. Write to outbox
        9. Commit transaction
        10. Release lock

        Args:
            user_id: User identifier
            amount_cents: Payment amount in cents
            currency: Currency code (e.g., 'USD')
            metadata: Optional payment metadata
            db: Optional database session

        Returns:
            Dict[str, Any]: Payment response

        Raises:
            PaymentValidationError: If input validation fails
            PaymentError: If payment processing fails
        """
        correlation_id = uuid.uuid4()
        user_id_uuid = uuid.UUID(str(user_id))

        logger.info(
            "payment_creation_started",
            correlation_id=str(correlation_id),
            user_id=str(user_id),
            amount_cents=amount_cents,
            currency=currency,
        )

        # Step 1: Validate input
        self._validate_payment_request(user_id_uuid, amount_cents, currency)

        # Step 2: Generate idempotency key
        idempotency_key = IdempotencyManager.generate_key(
            user_id=user_id_uuid,
            amount_cents=amount_cents,
            currency=currency,
            metadata=metadata,
        )

        logger.info(
            "idempotency_key_generated",
            correlation_id=str(correlation_id),
            idempotency_key=idempotency_key,
        )

        # Step 3: Check idempotency cache
        if db is not None:
            cached_response = await self.idempotency_manager.check_idempotency(
                idempotency_key, db
            )
            if cached_response:
                logger.info(
                    "payment_idempotent_return",
                    correlation_id=str(correlation_id),
                    idempotency_key=idempotency_key,
                )
                return cached_response

        # Step 4: Acquire distributed lock
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
            logger.info(
                "payment_lock_acquired",
                correlation_id=str(correlation_id),
                lock_key=lock_key,
            )

            if db is None:
                raise PaymentError("Database session is required")

            # Step 5: Begin database transaction (already managed by FastAPI dependency)
            # Step 6: Create payment record
            payment_id = uuid.uuid4()
            payment = Payment(
                id=payment_id,
                idempotency_key=idempotency_key,
                user_id=user_id_uuid,
                amount_cents=amount_cents,
                currency=currency.upper(),
                status="pending",
                metadata=metadata,
                created_at=datetime.utcnow(),
                updated_at=datetime.utcnow(),
            )
            db.add(payment)
            await db.flush()  # Get the payment ID

            # Record creation event
            await self._record_payment_event(
                db=db,
                payment_id=payment_id,
                event_type="payment.created",
                event_data={
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "status": "pending",
                },
                correlation_id=correlation_id,
            )

            logger.info(
                "payment_record_created",
                correlation_id=str(correlation_id),
                payment_id=str(payment_id),
            )

            # Step 7: Call Stripe API
            try:
                payment.status = "processing"
                await db.flush()

                await self._record_payment_event(
                    db=db,
                    payment_id=payment_id,
                    event_type="payment.processing",
                    event_data={"status": "processing"},
                    correlation_id=correlation_id,
                )

                stripe_metadata = {
                    "payment_id": str(payment_id),
                    "user_id": str(user_id_uuid),
                    "correlation_id": str(correlation_id),
                }
                if metadata:
                    stripe_metadata.update(metadata)

                payment_intent = await self.stripe_client.create_payment_intent(
                    amount_cents=amount_cents,
                    currency=currency,
                    idempotency_key=idempotency_key,
                    metadata=stripe_metadata,
                )

                # Update payment with Stripe info
                payment.stripe_payment_intent_id = payment_intent.id
                payment.status = payment_intent.status
                await db.flush()

                logger.info(
                    "stripe_payment_intent_created",
                    correlation_id=str(correlation_id),
                    payment_id=str(payment_id),
                    payment_intent_id=payment_intent.id,
                    status=payment_intent.status,
                )

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

            except StripeError as e:
                error_message = str(e)
                payment.status = "failed"
                payment.error_message = error_message
                await db.flush()

                await self._record_payment_event(
                    db=db,
                    payment_id=payment_id,
                    event_type="payment.failed",
                    event_data={
                        "error": error_message,
                        "error_type": e.error_type.value,
                    },
                    correlation_id=correlation_id,
                )

                logger.error(
                    "stripe_payment_failed",
                    correlation_id=str(correlation_id),
                    payment_id=str(payment_id),
                    error=error_message,
                    error_type=e.error_type.value,
                )

                # Don't retry permanent errors
                if e.error_type == StripeErrorType.PERMANENT:
                    await db.commit()
                    raise PaymentError(f"Payment failed: {error_message}")
                else:
                    raise

            # Step 8: Write to outbox
            await self._write_outbox_event(
                db=db,
                aggregate_id=payment_id,
                aggregate_type="payment",
                event_type="payment.created",
                payload={
                    "payment_id": str(payment_id),
                    "user_id": str(user_id_uuid),
                    "amount_cents": amount_cents,
                    "currency": currency,
                    "status": payment.status,
                    "stripe_payment_intent_id": payment.stripe_payment_intent_id,
                    "created_at": payment.created_at.isoformat(),
                },
            )

            # Step 9: Commit transaction
            await db.commit()

            logger.info(
                "payment_created_successfully",
                correlation_id=str(correlation_id),
                payment_id=str(payment_id),
                status=payment.status,
            )

            # Prepare response
            response = {
                "id": str(payment.id),
                "user_id": str(payment.user_id),
                "amount_cents": payment.amount_cents,
                "currency": payment.currency,
                "status": payment.status,
                "stripe_payment_intent_id": payment.stripe_payment_intent_id,
                "idempotency_key": payment.idempotency_key,
                "created_at": payment.created_at.isoformat(),
            }

            # Cache response for idempotency
            await self.idempotency_manager.store_response(idempotency_key, response)

            return response

        finally:
            # Step 10: Release lock
            redlock.unlock(lock)
            logger.info(
                "payment_lock_released",
                correlation_id=str(correlation_id),
                lock_key=lock_key,
            )

    async def get_payment_status(
        self, payment_id: str | uuid.UUID, db: AsyncSession
    ) -> Optional[Dict[str, Any]]:
        """
        Get payment status by ID.

        Args:
            payment_id: Payment ID
            db: Database session

        Returns:
            Optional[Dict[str, Any]]: Payment data or None if not found
        """
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
        amount_cents: Optional[int] = None,
        reason: Optional[str] = None,
        db: Optional[AsyncSession] = None,
    ) -> Dict[str, Any]:
        """
        Refund a payment.

        Args:
            payment_id: Payment ID
            amount_cents: Optional partial refund amount
            reason: Optional refund reason
            db: Database session

        Returns:
            Dict[str, Any]: Refund response

        Raises:
            PaymentError: If refund fails
        """
        if db is None:
            raise PaymentError("Database session is required")

        payment_id_uuid = uuid.UUID(str(payment_id))
        correlation_id = uuid.uuid4()

        logger.info(
            "refund_started",
            correlation_id=str(correlation_id),
            payment_id=str(payment_id),
        )

        # Get payment
        stmt = select(Payment).where(Payment.id == payment_id_uuid)
        result = await db.execute(stmt)
        payment = result.scalar_one_or_none()

        if payment is None:
            raise PaymentError(f"Payment {payment_id} not found")

        if payment.status != "succeeded":
            raise PaymentError(f"Cannot refund payment with status: {payment.status}")

        if not payment.stripe_payment_intent_id:
            raise PaymentError("Payment has no Stripe PaymentIntent ID")

        # Create refund in Stripe
        try:
            refund = await self.stripe_client.create_refund(
                payment_intent_id=payment.stripe_payment_intent_id,
                amount_cents=amount_cents,
                reason=reason,
                idempotency_key=f"refund:{payment_id}:{correlation_id}",
            )

            # Update payment status
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

            logger.info(
                "refund_created_successfully",
                correlation_id=str(correlation_id),
                payment_id=str(payment_id),
                refund_id=refund.id,
            )

            return {
                "payment_id": str(payment_id),
                "refund_id": refund.id,
                "status": refund.status,
                "amount_cents": refund.amount,
            }

        except StripeError as e:
            logger.error(
                "refund_failed",
                correlation_id=str(correlation_id),
                payment_id=str(payment_id),
                error=str(e),
            )
            raise PaymentError(f"Refund failed: {str(e)}")
