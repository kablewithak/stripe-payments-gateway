"""
Stripe webhook handler with signature verification and event deduplication.

Implements:
- Webhook signature verification
- Event deduplication using Redis
- Async event processing
- Retry logic for failed events
- Event Sourcing (Audit Logging)
"""
import hashlib
import uuid  # Critical for generating correlation_ids
from typing import Any, Callable, Dict, Optional

import redis.asyncio as aioredis
import stripe
import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

# Architecture: Import both the Whiteboard (Payment) and the Receipt (PaymentEvent)
from database.models import Payment, PaymentEvent
from config import get_settings

logger = structlog.get_logger(__name__)


class WebhookError(Exception):
    """Raised when webhook processing fails."""
    pass


class WebhookHandler:
    """
    Handles Stripe webhook events with deduplication and processing.
    """

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        self.settings = get_settings()
        self.redis_client = redis_client
        self._redis_initialized = False
        self.event_handlers: Dict[str, Callable] = {}
        logger.info("webhook_handler_initialized")

    async def _ensure_redis(self) -> aioredis.Redis:
        if self.redis_client is None or not self._redis_initialized:
            self.redis_client = await aioredis.from_url(
                self.settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            self._redis_initialized = True
        return self.redis_client

    def register_handler(self, event_type: str, handler: Callable) -> None:
        self.event_handlers[event_type] = handler
        logger.info("webhook_handler_registered", event_type=event_type)

    def verify_signature(
        self, payload: bytes, signature: str, secret: Optional[str] = None
    ) -> stripe.Event:
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
        try:
            redis = await self._ensure_redis()
            key = f"webhook:processed:{event_id}"
            exists = await redis.exists(key)
            return bool(exists)
        except Exception as e:
            logger.warning("webhook_dedup_check_error", error=str(e), event_id=event_id)
            return False

    async def mark_event_processed(
        self, event_id: str, ttl_seconds: int = 86400 * 7
    ) -> None:
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
        event_id = event.id
        event_type = event.type
        event_data = event.data.object

        logger.info(
            "processing_webhook_event",
            event_id=event_id,
            event_type=event_type,
        )

        if await self.is_event_processed(event_id):
            logger.info("webhook_event_already_processed", event_id=event_id)
            return {
                "status": "duplicate",
                "event_id": event_id,
                "message": "Event already processed",
            }

        handler = self.event_handlers.get(event_type)
        if handler is None:
            logger.warning("webhook_no_handler", event_type=event_type)
            await self.mark_event_processed(event_id)
            return {
                "status": "no_handler",
                "event_id": event_id,
                "message": f"No handler registered for event type: {event_type}",
            }

        try:
            if db is not None:
                result = await handler(event_data, db)
            else:
                result = await handler(event_data)

            await self.mark_event_processed(event_id)
            
            logger.info("webhook_event_processed_successfully", event_id=event_id)
            return {
                "status": "success",
                "event_id": event_id,
                "result": result,
            }

        except Exception as e:
            logger.error("webhook_event_processing_failed", error=str(e))
            raise WebhookError(f"Failed to process event {event_id}: {str(e)}")

    # ==========================================
    # HANDLERS
    # ==========================================

    async def handle_payment_intent_succeeded(
        self, payment_intent: Dict[str, Any], db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Handle payment_intent.succeeded event using Event Sourcing.
        1. Find the Payment.
        2. Create a PaymentEvent (Receipt).
        3. Update Payment status (Snapshot).
        4. Commit atomically.
        """
        payment_intent_id = payment_intent["id"]
        amount = payment_intent["amount"]
        currency = payment_intent["currency"]

        logger.info(
            "handling_payment_intent_succeeded",
            payment_intent_id=payment_intent_id,
            amount=amount,
        )

        # 1. Find the Record (The Whiteboard)
        query = select(Payment).where(Payment.stripe_payment_intent_id == payment_intent_id)
        result = await db.execute(query)
        payment_record = result.scalar_one_or_none()

        if not payment_record:
            # TODO: Senior Challenge - Should we CREATE it here if missing?
            logger.error("payment_not_found", payment_intent_id=payment_intent_id)
            return {"status": "error", "message": "Payment not found"}

        # 2. Write the Receipt (Event Sourcing)
        # This is our permanent, audit-proof history.
        new_event = PaymentEvent(
            payment_id=payment_record.id,
            event_type="payment_intent.succeeded",
            event_data=payment_intent,
            correlation_id=uuid.uuid4()
        )
        db.add(new_event)

        # 3. Update the Snapshot
        # We update the status so the frontend UI is fast.
        payment_record.status = "succeeded"

        # 4. Atomic Commit
        # If DB crashes here, BOTH fail. No partial state.
        await db.commit()

        return {
            "payment_intent_id": payment_intent_id,
            "status": "succeeded",
            "audit_log": True
        }

    async def handle_payment_intent_payment_failed(
        self, payment_intent: Dict[str, Any], db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Handle payment failure.
        """
        payment_intent_id = payment_intent["id"]
        error_message = payment_intent.get("last_payment_error", {}).get("message", "Unknown error")

        logger.info(
            "handling_payment_intent_failed",
            payment_intent_id=payment_intent_id,
            error=error_message,
        )

        # Ideally, we should add a PaymentEvent here too (e.g. "payment.failed"),
        # but for this specific lesson, we focus on the simple update.
        stmt = (
            update(Payment)
            .where(Payment.stripe_payment_intent_id == payment_intent_id)
            .values(status="failed", error_message=error_message)
        )
        result = await db.execute(stmt)
        await db.commit()

        return {
            "payment_intent_id": payment_intent_id,
            "status": "failed",
            "rows_updated": result.rowcount,
        }

    async def handle_charge_refunded(
        self, charge: Dict[str, Any], db: AsyncSession
    ) -> Dict[str, Any]:
        """
        Handle refund.
        """
        payment_intent_id = charge.get("payment_intent")
        if not payment_intent_id:
            return {"status": "skipped", "reason": "No payment_intent associated"}

        logger.info("handling_charge_refunded", payment_intent_id=payment_intent_id)

        stmt = (
            update(Payment)
            .where(Payment.stripe_payment_intent_id == payment_intent_id)
            .values(status="refunded")
        )
        result = await db.execute(stmt)
        await db.commit()

        return {
            "payment_intent_id": payment_intent_id,
            "status": "refunded",
            "rows_updated": result.rowcount,
        }

    async def close(self) -> None:
        if self.redis_client and self._redis_initialized:
            await self.redis_client.close()