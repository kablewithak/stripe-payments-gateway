"""
Stripe webhook handler with signature verification and event deduplication.

Implements:
- Webhook signature verification
- Event deduplication using Redis
- Async event processing
- Graceful degradation when Redis is unavailable
- Event sourcing / audit logging for payment lifecycle updates
"""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Awaitable, Callable

import redis.asyncio as aioredis
import stripe
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database.models import Payment, PaymentEvent

logger = structlog.get_logger(__name__)

HandlerResult = dict[str, Any]
WebhookHandlerFunc = Callable[..., Awaitable[HandlerResult]]


class WebhookError(Exception):
    """Base exception for webhook failures."""


class WebhookVerificationError(WebhookError):
    """Raised when webhook signature verification fails."""


class WebhookProcessingError(WebhookError):
    """Raised when webhook event processing fails."""


class WebhookHandler:
    """
    Handles Stripe webhook events with deduplication and processing.
    """

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
        self.settings = get_settings()
        self.redis_client = redis_client
        self._redis_initialized = redis_client is not None
        self.event_handlers: dict[str, WebhookHandlerFunc] = {}
        logger.info(
            "webhook_handler_initialized",
            redis_client_injected=redis_client is not None,
        )

    async def _ensure_redis(self) -> aioredis.Redis:
        """
        Ensure a Redis client exists.

        Important:
        - If a Redis client was injected, keep using it.
        - Only create a new client when no client exists.
        """
        if self.redis_client is None:
            self.redis_client = await aioredis.from_url(
                self.settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            self._redis_initialized = True
            logger.info("webhook_handler_redis_initialized_from_settings")
            return self.redis_client

        if not self._redis_initialized:
            self._redis_initialized = True
            logger.info("webhook_handler_redis_marked_initialized")

        return self.redis_client

    @staticmethod
    def _processed_event_key(event_id: str) -> str:
        """Build Redis key for processed webhook deduplication."""
        return f"webhook:processed:{event_id}"

    @staticmethod
    def _utcnow() -> datetime:
        """Centralized UTC timestamp helper."""
        return datetime.utcnow()

    async def _rollback_safely(self, db: AsyncSession, event_id: str) -> None:
        """Best-effort rollback helper that does not mask the original error."""
        try:
            await db.rollback()
        except Exception as exc:
            logger.warning(
                "webhook_db_rollback_failed",
                event_id=event_id,
                error=str(exc),
            )

    async def _get_payment_by_intent_id(
        self,
        payment_intent_id: str,
        db: AsyncSession,
    ) -> Payment | None:
        """Fetch a payment by Stripe PaymentIntent ID."""
        query = select(Payment).where(Payment.stripe_payment_intent_id == payment_intent_id)
        result = await db.execute(query)
        return result.scalar_one_or_none()

    async def _record_payment_event(
        self,
        db: AsyncSession,
        payment_id: uuid.UUID,
        event_type: str,
        event_data: dict[str, Any],
    ) -> None:
        """Persist an immutable audit event."""
        event = PaymentEvent(
            payment_id=payment_id,
            event_type=event_type,
            event_data=event_data,
            correlation_id=uuid.uuid4(),
            created_at=self._utcnow(),
        )
        db.add(event)

    @staticmethod
    def _set_payment_status(
        payment: Payment,
        *,
        status: str,
        error_message: str | None = None,
    ) -> None:
        """Apply consistent payment state updates."""
        payment.status = status
        if hasattr(payment, "updated_at"):
            payment.updated_at = datetime.utcnow()
        if error_message is not None:
            payment.error_message = error_message
        elif hasattr(payment, "error_message") and status in {"succeeded", "refunded"}:
            payment.error_message = None

    def register_handler(self, event_type: str, handler: WebhookHandlerFunc) -> None:
        """Register an async handler for a Stripe event type."""
        self.event_handlers[event_type] = handler
        logger.info("webhook_handler_registered", event_type=event_type)

    def verify_signature(
        self,
        payload: bytes,
        signature: str,
        secret: str | None = None,
    ) -> stripe.Event:
        """Verify Stripe webhook signature and return the parsed event."""
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

        except stripe.error.SignatureVerificationError as exc:
            logger.error("webhook_signature_verification_failed", error=str(exc))
            raise WebhookVerificationError(f"Invalid webhook signature: {exc}") from exc

        except Exception as exc:
            logger.error("webhook_verification_error", error=str(exc))
            raise WebhookVerificationError(f"Webhook verification failed: {exc}") from exc

    async def is_event_processed(self, event_id: str) -> bool:
        """
        Check whether a webhook event has already been processed.

        Degrades gracefully when Redis is unavailable.
        """
        try:
            redis_client = await self._ensure_redis()
            exists = await redis_client.exists(self._processed_event_key(event_id))
            return bool(exists)
        except Exception as exc:
            logger.warning(
                "webhook_dedup_check_error",
                event_id=event_id,
                error=str(exc),
            )
            return False

    async def mark_event_processed(
        self,
        event_id: str,
        ttl_seconds: int = 86400 * 7,
    ) -> None:
        """
        Mark a webhook event as processed.

        Degrades gracefully when Redis is unavailable.
        """
        try:
            redis_client = await self._ensure_redis()
            await redis_client.setex(
                self._processed_event_key(event_id),
                ttl_seconds,
                "1",
            )
            logger.info("webhook_marked_processed", event_id=event_id)
        except Exception as exc:
            logger.warning(
                "webhook_mark_processed_error",
                event_id=event_id,
                error=str(exc),
            )

    async def process_event(
        self,
        event: stripe.Event,
        db: AsyncSession | None = None,
    ) -> dict[str, Any]:
        """
        Process a Stripe webhook event with deduplication.

        Marks events as processed only after successful handler execution
        or when no handler exists for the event type.
        """
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

            logger.info(
                "webhook_event_processed_successfully",
                event_id=event_id,
                event_type=event_type,
            )
            return {
                "status": "success",
                "event_id": event_id,
                "result": result,
            }

        except WebhookError:
            raise

        except Exception as exc:
            logger.error(
                "webhook_event_processing_failed",
                event_id=event_id,
                event_type=event_type,
                error=str(exc),
            )
            raise WebhookProcessingError(
                f"Failed to process event {event_id}: {exc}"
            ) from exc

    async def handle_payment_intent_succeeded(
        self,
        payment_intent: dict[str, Any],
        db: AsyncSession,
    ) -> dict[str, Any]:
        """
        Handle payment_intent.succeeded.

        Writes an audit event and updates payment snapshot atomically.
        """
        payment_intent_id = payment_intent["id"]
        amount = payment_intent["amount"]
        currency = payment_intent["currency"]

        logger.info(
            "handling_payment_intent_succeeded",
            payment_intent_id=payment_intent_id,
            amount=amount,
            currency=currency,
        )

        try:
            payment_record = await self._get_payment_by_intent_id(payment_intent_id, db)

            if payment_record is None:
                logger.warning(
                    "webhook_payment_not_found_for_success",
                    payment_intent_id=payment_intent_id,
                )
                return {
                    "status": "not_found",
                    "payment_intent_id": payment_intent_id,
                    "message": "Payment not found",
                }

            await self._record_payment_event(
                db=db,
                payment_id=payment_record.id,
                event_type="payment_intent.succeeded",
                event_data=payment_intent,
            )

            self._set_payment_status(payment_record, status="succeeded")

            await db.commit()

            return {
                "status": "succeeded",
                "payment_intent_id": payment_intent_id,
                "payment_id": str(payment_record.id),
                "audit_log": True,
            }

        except Exception:
            await self._rollback_safely(db, payment_intent_id)
            raise

    async def handle_payment_intent_payment_failed(
        self,
        payment_intent: dict[str, Any],
        db: AsyncSession,
    ) -> dict[str, Any]:
        """
        Handle payment_intent.payment_failed.

        Writes an audit event and updates payment snapshot atomically.
        """
        payment_intent_id = payment_intent["id"]
        error_message = payment_intent.get("last_payment_error", {}).get(
            "message",
            "Unknown error",
        )

        logger.info(
            "handling_payment_intent_failed",
            payment_intent_id=payment_intent_id,
            error=error_message,
        )

        try:
            payment_record = await self._get_payment_by_intent_id(payment_intent_id, db)

            if payment_record is None:
                logger.warning(
                    "webhook_payment_not_found_for_failure",
                    payment_intent_id=payment_intent_id,
                )
                return {
                    "status": "not_found",
                    "payment_intent_id": payment_intent_id,
                    "message": "Payment not found",
                }

            await self._record_payment_event(
                db=db,
                payment_id=payment_record.id,
                event_type="payment_intent.payment_failed",
                event_data=payment_intent,
            )

            self._set_payment_status(
                payment_record,
                status="failed",
                error_message=error_message,
            )

            await db.commit()

            return {
                "status": "failed",
                "payment_intent_id": payment_intent_id,
                "payment_id": str(payment_record.id),
                "error_message": error_message,
                "audit_log": True,
            }

        except Exception:
            await self._rollback_safely(db, payment_intent_id)
            raise

    async def handle_charge_refunded(
        self,
        charge: dict[str, Any],
        db: AsyncSession,
    ) -> dict[str, Any]:
        """
        Handle charge.refunded.

        Writes an audit event and updates payment snapshot atomically.
        """
        payment_intent_id = charge.get("payment_intent")
        if not payment_intent_id:
            logger.warning("webhook_refund_missing_payment_intent")
            return {
                "status": "skipped",
                "reason": "No payment_intent associated",
            }

        logger.info("handling_charge_refunded", payment_intent_id=payment_intent_id)

        try:
            payment_record = await self._get_payment_by_intent_id(payment_intent_id, db)

            if payment_record is None:
                logger.warning(
                    "webhook_payment_not_found_for_refund",
                    payment_intent_id=payment_intent_id,
                )
                return {
                    "status": "not_found",
                    "payment_intent_id": payment_intent_id,
                    "message": "Payment not found",
                }

            await self._record_payment_event(
                db=db,
                payment_id=payment_record.id,
                event_type="charge.refunded",
                event_data=charge,
            )

            self._set_payment_status(payment_record, status="refunded")

            await db.commit()

            return {
                "status": "refunded",
                "payment_intent_id": payment_intent_id,
                "payment_id": str(payment_record.id),
                "audit_log": True,
            }

        except Exception:
            await self._rollback_safely(db, payment_intent_id)
            raise

    async def close(self) -> None:
        """Close Redis client if this handler has one."""
        if self.redis_client is None or not self._redis_initialized:
            return

        try:
            await self.redis_client.aclose()
        except AttributeError:
            await self.redis_client.close()