"""
API routes for payment processing.
"""
from __future__ import annotations

import time
from datetime import date, timedelta
from typing import Any

import structlog
from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response, status
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from sqlalchemy.ext.asyncio import AsyncSession

from core.payment_processor import (
    PaymentConflictError,
    PaymentError,
    PaymentFailedError,
    PaymentNotFoundError,
    PaymentProcessor,
    PaymentProviderError,
    PaymentValidationError,
    RefundNotAllowedError,
)
from core.reconciliation import ReconciliationEngine
from database.connection import get_db
from integrations.webhook_handler import WebhookError, WebhookHandler
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

payment_router = APIRouter(prefix="/payments", tags=["payments"])
webhook_router = APIRouter(prefix="/webhooks", tags=["webhooks"])
admin_router = APIRouter(prefix="/admin", tags=["admin"])
monitoring_router = APIRouter(tags=["monitoring"])

payment_processor = PaymentProcessor()
webhook_handler = WebhookHandler()
health_check = HealthCheck()
reconciliation_engine = ReconciliationEngine()

webhook_handler.register_handler(
    "payment_intent.succeeded",
    webhook_handler.handle_payment_intent_succeeded,
)
webhook_handler.register_handler(
    "payment_intent.payment_failed",
    webhook_handler.handle_payment_intent_payment_failed,
)
webhook_handler.register_handler(
    "charge.refunded",
    webhook_handler.handle_charge_refunded,
)


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
) -> dict[str, Any]:
    """
    Create a new payment.

    This endpoint is idempotent. Duplicate requests return the same payment.
    """
    start_time = time.time()

    try:
        logger.info(
            "api_create_payment_request",
            user_id=str(request.user_id),
            amount_cents=request.amount_cents,
            currency=request.currency,
        )

        payment = await payment_processor.create_payment(
            user_id=request.user_id,
            amount_cents=request.amount_cents,
            currency=request.currency,
            metadata=request.metadata,
            db=db,
        )

        duration = time.time() - start_time
        metrics.record_payment_request(
            payment["status"],
            request.currency,
            request.amount_cents,
        )
        metrics.record_payment_duration(duration)

        logger.info(
            "api_create_payment_success",
            payment_id=payment["id"],
            status=payment["status"],
            duration_seconds=duration,
        )
        return payment

    except PaymentValidationError as exc:
        logger.warning("api_create_payment_validation_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except PaymentConflictError as exc:
        logger.warning("api_create_payment_conflict", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    except PaymentFailedError as exc:
        logger.warning("api_create_payment_failed", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=str(exc),
        ) from exc

    except PaymentProviderError as exc:
        logger.error("api_create_payment_provider_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    except PaymentError as exc:
        logger.error("api_create_payment_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Payment creation failed",
        ) from exc

    except Exception as exc:
        logger.error("api_create_payment_unexpected_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="An unexpected error occurred",
        ) from exc


@payment_router.get(
    "/{payment_id}",
    response_model=PaymentStatusResponse,
    summary="Get payment status",
    description="Retrieve the current status of a payment",
)
async def get_payment_status(
    payment_id: str,
    db: AsyncSession = Depends(get_db),
) -> dict[str, Any]:
    """Get payment status by ID."""
    try:
        payment = await payment_processor.get_payment_status(payment_id, db)

        if payment is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail="Payment not found",
            )

        return payment

    except PaymentValidationError as exc:
        logger.warning(
            "api_get_payment_status_validation_error",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except HTTPException:
        raise

    except Exception as exc:
        logger.error(
            "api_get_payment_status_error",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to retrieve payment status",
        ) from exc


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
) -> dict[str, Any]:
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

    except PaymentValidationError as exc:
        logger.warning(
            "api_refund_payment_validation_error",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except PaymentNotFoundError as exc:
        logger.warning(
            "api_refund_payment_not_found",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail=str(exc),
        ) from exc

    except RefundNotAllowedError as exc:
        logger.warning(
            "api_refund_payment_not_allowed",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=str(exc),
        ) from exc

    except PaymentProviderError as exc:
        logger.error(
            "api_refund_payment_provider_error",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=str(exc),
        ) from exc

    except PaymentError as exc:
        logger.error(
            "api_refund_payment_error",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Refund failed",
        ) from exc

    except Exception as exc:
        logger.error(
            "api_refund_payment_unexpected_error",
            payment_id=payment_id,
            error=str(exc),
        )
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Refund failed",
        ) from exc


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
) -> dict[str, Any]:
    """
    Handle Stripe webhook events.

    Verifies signature and processes events with deduplication.
    """
    start_time = time.time()

    try:
        body = await request.body()
        event = webhook_handler.verify_signature(body, stripe_signature)

        logger.info(
            "api_webhook_received",
            event_id=event.id,
            event_type=event.type,
        )

        result = await webhook_handler.process_event(event, db)

        duration = time.time() - start_time
        metrics.record_webhook_event(event.type, result["status"], duration)

        return result

    except WebhookError as exc:
        logger.error("api_webhook_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
        ) from exc

    except Exception as exc:
        logger.error("api_webhook_unexpected_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Webhook processing failed",
        ) from exc


@admin_router.post(
    "/reconcile",
    response_model=ReconciliationResponse,
    summary="Run reconciliation",
    description="Manually trigger reconciliation for a specific date",
)
async def run_reconciliation(
    reconciliation_date: str | None = None,
) -> dict[str, Any]:
    """
    Run reconciliation for a specific date.

    If no date is provided, reconcile yesterday.
    """
    try:
        if reconciliation_date:
            recon_date = date.fromisoformat(reconciliation_date)
        else:
            recon_date = date.today() - timedelta(days=1)

        logger.info("api_reconciliation_started", date=recon_date.isoformat())

        result = await reconciliation_engine.reconcile_date(recon_date)

        logger.info(
            "api_reconciliation_completed",
            date=recon_date.isoformat(),
            discrepancies=len(result["discrepancies"]),
        )
        return result

    except Exception as exc:
        logger.error("api_reconciliation_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail=f"Reconciliation failed: {str(exc)}",
        ) from exc


@monitoring_router.get(
    "/health",
    response_model=HealthCheckResponse,
    summary="Health check",
    description="Check overall system health",
)
async def health() -> dict[str, Any]:
    """Health check endpoint for monitoring."""
    try:
        return await health_check.check_all()
    except Exception as exc:
        logger.error("health_check_error", error=str(exc))
        return {
            "status": "unhealthy",
            "checks": {"error": str(exc)},
        }


@monitoring_router.get(
    "/health/live",
    response_model=HealthCheckResponse,
    summary="Liveness probe",
    description="Kubernetes liveness probe endpoint",
)
async def liveness() -> dict[str, Any]:
    """Liveness probe endpoint."""
    return await health_check.liveness()


@monitoring_router.get(
    "/health/ready",
    response_model=HealthCheckResponse,
    summary="Readiness probe",
    description="Kubernetes readiness probe endpoint",
)
async def readiness() -> dict[str, Any]:
    """Readiness probe endpoint."""
    try:
        result = await health_check.readiness()
        if result["status"] != "healthy":
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=result,
            )
        return result

    except HTTPException:
        raise

    except Exception as exc:
        logger.error("readiness_check_error", error=str(exc))
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail={"status": "unhealthy", "error": str(exc)},
        ) from exc


@monitoring_router.get(
    "/metrics",
    summary="Prometheus metrics",
    description="Expose Prometheus metrics",
    include_in_schema=False,
)
async def prometheus_metrics() -> Response:
    """Expose Prometheus metrics."""
    return Response(
        content=generate_latest(),
        media_type=CONTENT_TYPE_LATEST,
    )