"""
API routes for payment processing.
"""
import time
from datetime import date
from typing import Any, Dict

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from core.payment_processor import PaymentError, PaymentProcessor, PaymentValidationError
from core.reconciliation import ReconciliationEngine
from database.connection import get_db
from integrations.webhook_handler import WebhookHandler, WebhookError
from monitoring.health import HealthCheck
from monitoring.metrics import metrics

from .schemas import (
    CreatePaymentRequest,
    CreatePaymentResponse,
    HealthCheckResponse,
    PaymentStatusResponse,
    ReconciliationResponse,
    RefundRequest,
    RefundResponse,
    WebhookResponse,
)

logger = structlog.get_logger(__name__)

# Create routers
payment_router = APIRouter(prefix="/payments", tags=["payments"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
monitoring_router = APIRouter(tags=["monitoring"])

# Initialize services
payment_processor = PaymentProcessor()
webhook_handler = WebhookHandler()
health_check = HealthCheck()
reconciliation_engine = ReconciliationEngine()

# Register webhook handlers
webhook_handler.register_handler(
    "payment_intent.succeeded", webhook_handler.handle_payment_intent_succeeded
)
webhook_handler.register_handler(
    "payment_intent.payment_failed", webhook_handler.handle_payment_intent_payment_failed
)
webhook_handler.register_handler("charge.refunded", webhook_handler.handle_charge_refunded)


@payment_router.post(
    "",
    response_model=CreatePaymentResponse,
    status_code=status.HTTP_201_CREATED,
    summary="Create a payment",
    description="Create a new payment with idempotency guarantees",
)
async def create_payment(
    request: CreatePaymentRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Create a new payment.

    This endpoint is idempotent - duplicate requests will return the same payment.
    """
    start_time = time.time()

    try:
        logger.info(
            "api_create_payment_request",
            user_id=request.user_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
        )

        # Create payment
        payment = await payment_processor.create_payment(
            user_id=request.user_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            metadata=request.metadata,
            db=db,
        )

        # Record metrics
        duration = time.time() - start_time
        metrics.record_payment_request(payment["status"], request.currency, request.amount_cents)
        metrics.record_payment_duration(duration)

        logger.info(
            "api_create_payment_success",
            payment_id=payment["id"],
            status=payment["status"],
            duration_seconds=duration,
        )

        return payment

    except PaymentValidationError as e:
        logger.warning("api_create_payment_validation_error", error=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    except PaymentError as e:
        logger.error("api_create_payment_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Payment creation failed: {str(e)}",
        )

    except Exception as e:
        logger.error("api_create_payment_unexpected_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        )


@payment_router.get(
    "/{payment_id}",
    response_model=PaymentStatusResponse,
    summary="Get payment status",
    description="Retrieve the current status of a payment",
)
async def get_payment_status(
    payment_id: str,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Get payment status by ID."""
    try:
        payment = await payment_processor.get_payment_status(payment_id, db)

        if payment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="Payment not found"
            )

        return payment

    except HTTPException:
        raise
    except Exception as e:
        logger.error("api_get_payment_status_error", payment_id=payment_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve payment status",
        )


@payment_router.post(
    "/{payment_id}/refund",
    response_model=RefundResponse,
    summary="Refund a payment",
    description="Create a full or partial refund for a payment",
)
async def refund_payment(
    payment_id: str,
    request: RefundRequest,
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """Refund a payment."""
    try:
        logger.info(
            "api_refund_payment_request",
            payment_id=payment_id,
            amount_cents=request.amount_cents,
            reason=request.reason,
        )

        refund = await payment_processor.refund_payment(
            payment_id=payment_id,
            amount_cents=request.amount_cents,
            reason=request.reason,
            db=db,
        )

        logger.info(
            "api_refund_payment_success",
            payment_id=payment_id,
            refund_id=refund["refund_id"],
        )

        return refund

    except PaymentError as e:
        logger.warning("api_refund_payment_error", payment_id=payment_id, error=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    except Exception as e:
        logger.error("api_refund_payment_unexpected_error", payment_id=payment_id, error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Refund failed"
        )


@webhook_router.post(
    "/stripe",
    response_model=WebhookResponse,
    summary="Stripe webhook endpoint",
    description="Handle Stripe webhook events",
)
async def stripe_webhook(
    request: Request,
    stripe_signature: str = Header(..., alias="Stripe-Signature"),
    db: AsyncSession = Depends(get_db),
) -> Dict[str, Any]:
    """
    Handle Stripe webhook events.

    Verifies signature and processes events with deduplication.
    """
    start_time = time.time()

    try:
        # Get raw body
        body = await request.body()

        # Verify signature
        event = webhook_handler.verify_signature(body, stripe_signature)

        logger.info(
            "api_webhook_received",
            event_id=event.id,
            event_type=event.type,
        )

        # Process event
        result = await webhook_handler.process_event(event, db)

        # Record metrics
        duration = time.time() - start_time
        metrics.record_webhook_event(event.type, result["status"], duration)

        return result

    except WebhookError as e:
        logger.error("api_webhook_error", error=str(e))
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(e))

    except Exception as e:
        logger.error("api_webhook_unexpected_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed",
        )


@admin_router.post(
    "/reconcile",
    response_model=ReconciliationResponse,
    summary="Run reconciliation",
    description="Manually trigger reconciliation for a specific date",
)
async def run_reconciliation(
    reconciliation_date: str | None = None,
) -> Dict[str, Any]:
    """
    Run reconciliation for a specific date.

    If no date provided, reconciles yesterday.
    """
    try:
        if reconciliation_date:
            recon_date = date.fromisoformat(reconciliation_date)
        else:
            from datetime import timedelta

            recon_date = date.today() - timedelta(days=1)

        logger.info("api_reconciliation_started", date=recon_date.isoformat())

        result = await reconciliation_engine.reconcile_date(recon_date)

        logger.info(
            "api_reconciliation_completed",
            date=recon_date.isoformat(),
            discrepancies=len(result["discrepancies"]),
        )

        return result

    except Exception as e:
        logger.error("api_reconciliation_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reconciliation failed: {str(e)}",
        )


@monitoring_router.get(
    "/health",
    response_model=HealthCheckResponse,
    summary="Health check",
    description="Check overall system health",
)
async def health() -> Dict[str, Any]:
    """Health check endpoint for monitoring."""
    try:
        result = await health_check.check_all()
        return result
    except Exception as e:
        logger.error("health_check_error", error=str(e))
        return {
            "status": "unhealthy",
            "checks": {"error": str(e)},
        }


@monitoring_router.get(
    "/health/live",
    response_model=HealthCheckResponse,
    summary="Liveness probe",
    description="Kubernetes liveness probe endpoint",
)
async def liveness() -> Dict[str, Any]:
    """Liveness probe endpoint."""
    return await health_check.liveness()


@monitoring_router.get(
    "/health/ready",
    response_model=HealthCheckResponse,
    summary="Readiness probe",
    description="Kubernetes readiness probe endpoint",
)
async def readiness() -> Dict[str, Any]:
    """Readiness probe endpoint."""
    try:
        result = await health_check.readiness()
        if result["status"] != "healthy":
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=result)
        return result
    except HTTPException:
        raise
    except Exception as e:
        logger.error("readiness_check_error", error=str(e))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "error": str(e)},
        )


@monitoring_router.get(
    "/metrics",
    summary="Prometheus metrics",
    description="Expose Prometheus metrics",
    include_in_schema=False,  # Don't include in OpenAPI docs
)
async def prometheus_metrics() -> Response:
    """Expose Prometheus metrics."""
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)
