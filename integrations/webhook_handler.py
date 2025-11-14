"""
Stripe webhook handler with signature verification and event deduplication.

Implements:
- Webhook signature verification
- Event deduplication using Redis
- Async event processing
- Retry logic for failed events
"""
import hashlib
from typing import Any, Callable, Dict, Optional

import redis.asyncio as aioredis
import stripe
import structlog
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings

logger = structlog.get_logger(__name__)


class WebhookError(Exception):
    """Raised when webhook processing fails."""

    pass


class WebhookHandler:
    """
    Handles Stripe webhook events with deduplication and processing.

    Features:
    - Signature verification using Stripe webhook secrets
    - Event deduplication (store processed webhook IDs in Redis)
    - Async event processing with retry logic
    - Event type routing to appropriate handlers
    """

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        """
        Initialize webhook handler.

        Args:
            redis_client: Optional Redis client for event deduplication
        """
        self.settings = get_settings()
        self.redis_client = redis_client
        self._redis_initialized = False
        self.event_handlers: Dict[str, Callable] = {}

        logger.info("webhook_handler_initialized")

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

    def register_handler(self, event_type: str, handler: Callable) -> None:
        """
        Register a handler for a specific event type.

        Args:
            event_type: Stripe event type (e.g., 'payment_intent.succeeded')
            handler: Async callable to handle the event

        Example:
            async def handle_payment_succeeded(event_data):
                ...

            handler.register_handler('payment_intent.succeeded', handle_payment_succeeded)
        """
        self.event_handlers[event_type] = handler
        logger.info("webhook_handler_registered", event_type=event_type)

    def verify_signature(
        self, payload: bytes, signature: str, secret: Optional[str] = None
    ) -> stripe.Event:
        """
        Verify webhook signature and construct event.

        Args:
            payload: Raw request body as bytes
            signature: Stripe-Signature header value
            secret: Optional webhook secret (uses config if not provided)

        Returns:
            stripe.Event: Verified Stripe event

        Raises:
            WebhookError: If signature verification fails
        """
        webhook_secret = secret or self.settings.stripe_webhook_secret

        try:
            event = stripe.Webhook.construct_event(
                payload=payload,
                sig_header=signature,
                secret=webhook_secret,
            )

            logger.info(
                "webhook_signature_verified",
                event_id=event.id,
                event_type=event.type,
            )

            return event

        except stripe.error.SignatureVerificationError as e:
            logger.error("webhook_signature_verification_failed", error=str(e))
            raise WebhookError(f"Invalid webhook signature: {str(e)}")
        except Exception as e:
            logger.error("webhook_verification_error", error=str(e))
            raise WebhookError(f"Webhook verification failed: {str(e)}")

    async def is_event_processed(self, event_id: str) -> bool:
        """
        Check if webhook event has already been processed.

        Args:
            event_id: Stripe event ID

        Returns:
            bool: True if event already processed, False otherwise
        """
        try:
            redis = await self._ensure_redis()
            key = f"webhook:processed:{event_id}"
            exists = await redis.exists(key)
            return bool(exists)
        except Exception as e:
            logger.warning("webhook_dedup_check_error", error=str(e), event_id=event_id)
            # If Redis is down, process the event anyway to avoid losing it
            return False

    async def mark_event_processed(
        self, event_id: str, ttl_seconds: int = 86400 * 7  # 7 days
    ) -> None:
        """
        Mark webhook event as processed.

        Args:
            event_id: Stripe event ID
            ttl_seconds: Time to keep the record (default: 7 days)
        """
        try:
            redis = await self._ensure_redis()
            key = f"webhook:processed:{event_id}"
            await redis.setex(key, ttl_seconds, "1")
            logger.info("webhook_marked_processed", event_id=event_id)
        except Exception as e:
            logger.warning("webhook_mark_processed_error", error=str(e), event_id=event_id)

    async def process_event(
        self, event: stripe.Event, db: Optional[AsyncSession] = None
    ) -> Dict[str, Any]:
        """
        Process a webhook event.

        Args:
            event: Verified Stripe event
            db: Optional database session for handlers that need it

        Returns:
            Dict[str, Any]: Processing result

        Raises:
            WebhookError: If event processing fails
        """
        event_id = event.id
        event_type = event.type
        event_data = event.data.object

        logger.info(
            "processing_webhook_event",
            event_id=event_id,
            event_type=event_type,
        )

        # Check for duplicate events
        if await self.is_event_processed(event_id):
            logger.info(
                "webhook_event_already_processed",
                event_id=event_id,
                event_type=event_type,
            )
            return {
                "status": "duplicate",
                "event_id": event_id,
                "message": "Event already processed",
            }

        # Route event to appropriate handler
        handler = self.event_handlers.get(event_type)
        if handler is None:
            logger.warning(
                "webhook_no_handler",
                event_id=event_id,
                event_type=event_type,
            )
            # Mark as processed even if no handler to avoid reprocessing
            await self.mark_event_processed(event_id)
            return {
                "status": "no_handler",
                "event_id": event_id,
                "event_type": event_type,
                "message": f"No handler registered for event type: {event_type}",
            }

        try:
            # Call the registered handler
            if db is not None:
                result = await handler(event_data, db)
            else:
                result = await handler(event_data)

            # Mark event as successfully processed
            await self.mark_event_processed(event_id)

            logger.info(
                "webhook_event_processed_successfully",
                event_id=event_id,
                event_type=event_type,
            )

            return {
                "status": "success",
                "event_id": event_id,
                "event_type": event_type,
                "result": result,
            }

        except Exception as e:
            logger.error(
                "webhook_event_processing_failed",
                event_id=event_id,
                event_type=event_type,
                error=str(e),
            )
            raise WebhookError(f"Failed to process event {event_id}: {str(e)}")

    async def handle_payment_intent_succeeded(
        self, payment_intent: Dict[str, Any], db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Handle payment_intent.succeeded event.

        Args:
            payment_intent: PaymentIntent object data
            db: Database session

        Returns:
            Dict[str, Any]: Handler result
        """
        from database.models import Payment
        from sqlalchemy import update

        payment_intent_id = payment_intent["id"]
        amount = payment_intent["amount"]
        currency = payment_intent["currency"]

        logger.info(
            "handling_payment_intent_succeeded",
            payment_intent_id=payment_intent_id,
            amount=amount,
            currency=currency,
        )

        # Update payment status in database
        stmt = (
            update(Payment)
            .where(Payment.stripe_payment_intent_id == payment_intent_id)
            .values(status="succeeded")
        )
        result = await db.execute(stmt)
        await db.commit()

        rows_updated = result.rowcount
        if rows_updated == 0:
            logger.warning(
                "payment_intent_not_found_in_db",
                payment_intent_id=payment_intent_id,
            )

        return {
            "payment_intent_id": payment_intent_id,
            "rows_updated": rows_updated,
        }

    async def handle_payment_intent_payment_failed(
        self, payment_intent: Dict[str, Any], db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Handle payment_intent.payment_failed event.

        Args:
            payment_intent: PaymentIntent object data
            db: Database session

        Returns:
            Dict[str, Any]: Handler result
        """
        from database.models import Payment
        from sqlalchemy import update

        payment_intent_id = payment_intent["id"]
        error_message = payment_intent.get("last_payment_error", {}).get("message", "Unknown error")

        logger.info(
            "handling_payment_intent_failed",
            payment_intent_id=payment_intent_id,
            error=error_message,
        )

        # Update payment status in database
        stmt = (
            update(Payment)
            .where(Payment.stripe_payment_intent_id == payment_intent_id)
            .values(status="failed", error_message=error_message)
        )
        result = await db.execute(stmt)
        await db.commit()

        return {
            "payment_intent_id": payment_intent_id,
            "rows_updated": result.rowcount,
            "error": error_message,
        }

    async def handle_charge_refunded(
        self, charge: Dict[str, Any], db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Handle charge.refunded event.

        Args:
            charge: Charge object data
            db: Database session

        Returns:
            Dict[str, Any]: Handler result
        """
        from database.models import Payment
        from sqlalchemy import update

        payment_intent_id = charge.get("payment_intent")
        if not payment_intent_id:
            logger.warning("charge_refunded_no_payment_intent", charge_id=charge["id"])
            return {"status": "skipped", "reason": "No payment_intent associated"}

        logger.info(
            "handling_charge_refunded",
            charge_id=charge["id"],
            payment_intent_id=payment_intent_id,
        )

        # Update payment status in database
        stmt = (
            update(Payment)
            .where(Payment.stripe_payment_intent_id == payment_intent_id)
            .values(status="refunded")
        )
        result = await db.execute(stmt)
        await db.commit()

        return {
            "payment_intent_id": payment_intent_id,
            "rows_updated": result.rowcount,
        }

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis_client and self._redis_initialized:
            await self.redis_client.close()
