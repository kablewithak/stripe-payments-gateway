"""
Pydantic schemas for API request/response models.
"""
from datetime import datetime
from typing import Any, Dict, Optional
from uuid import UUID

from pydantic import BaseModel, Field, field_validator


class CreatePaymentRequest(BaseModel):
    """Request schema for creating a payment."""

    user_id: str = Field(..., description="User identifier")
    amount_cents: int = Field(..., gt=0, description="Payment amount in cents (minimum 50)")
    currency: str = Field(..., min_length=3, max_length=3, description="Currency code (e.g., USD)")
    metadata: Optional[Dict[str, Any]] = Field(default=None, description="Optional payment metadata")

    @field_validator("amount_cents")
    @classmethod
    def validate_amount(cls, v: int) -> int:
        """Validate minimum amount."""
        if v < 50:
            raise ValueError("Amount must be at least 50 cents (Stripe minimum)")
        return v

    @field_validator("currency")
    @classmethod
    def validate_currency(cls, v: str) -> str:
        """Validate currency format."""
        return v.upper()

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "user_id": "123e4567-e89b-12d3-a456-426614174000",
                    "amount_cents": 1000,
                    "currency": "USD",
                    "metadata": {"order_id": "order_123", "product": "Premium Subscription"},
                }
            ]
        }
    }


class CreatePaymentResponse(BaseModel):
    """Response schema for payment creation."""

    id: str = Field(..., description="Payment ID")
    user_id: str = Field(..., description="User identifier")
    amount_cents: int = Field(..., description="Payment amount in cents")
    currency: str = Field(..., description="Currency code")
    status: str = Field(..., description="Payment status")
    stripe_payment_intent_id: Optional[str] = Field(
        default=None, description="Stripe PaymentIntent ID"
    )
    idempotency_key: str = Field(..., description="Idempotency key")
    created_at: str = Field(..., description="Creation timestamp (ISO 8601)")

    model_config = {
        "json_schema_extra": {
            "examples": [
                {
                    "id": "123e4567-e89b-12d3-a456-426614174000",
                    "user_id": "123e4567-e89b-12d3-a456-426614174000",
                    "amount_cents": 1000,
                    "currency": "USD",
                    "status": "requires_payment_method",
                    "stripe_payment_intent_id": "pi_1234567890",
                    "idempotency_key": "user123:abc123:def456",
                    "created_at": "2025-01-06T10:00:00Z",
                }
            ]
        }
    }


class PaymentStatusResponse(BaseModel):
    """Response schema for payment status."""

    id: str = Field(..., description="Payment ID")
    user_id: str = Field(..., description="User identifier")
    amount_cents: int = Field(..., description="Payment amount in cents")
    currency: str = Field(..., description="Currency code")
    status: str = Field(..., description="Payment status")
    stripe_payment_intent_id: Optional[str] = Field(
        default=None, description="Stripe PaymentIntent ID"
    )
    error_message: Optional[str] = Field(default=None, description="Error message if failed")
    created_at: str = Field(..., description="Creation timestamp (ISO 8601)")
    updated_at: str = Field(..., description="Last update timestamp (ISO 8601)")


class RefundRequest(BaseModel):
    """Request schema for refunding a payment."""

    amount_cents: Optional[int] = Field(
        default=None, gt=0, description="Partial refund amount (full refund if not specified)"
    )
    reason: Optional[str] = Field(
        default=None, description="Refund reason (requested_by_customer, duplicate, fraudulent)"
    )

    model_config = {
        "json_schema_extra": {
            "examples": [
                {"amount_cents": 500, "reason": "requested_by_customer"},
                {"reason": "duplicate"},
            ]
        }
    }


class RefundResponse(BaseModel):
    """Response schema for refund."""

    payment_id: str = Field(..., description="Payment ID")
    refund_id: str = Field(..., description="Stripe Refund ID")
    status: str = Field(..., description="Refund status")
    amount_cents: int = Field(..., description="Refunded amount in cents")


class HealthCheckResponse(BaseModel):
    """Response schema for health checks."""

    status: str = Field(..., description="Overall health status (healthy/unhealthy)")
    checks: Optional[Dict[str, Any]] = Field(default=None, description="Individual service checks")
    message: Optional[str] = Field(default=None, description="Status message")


class WebhookResponse(BaseModel):
    """Response schema for webhook processing."""

    status: str = Field(..., description="Processing status")
    event_id: str = Field(..., description="Stripe event ID")
    message: Optional[str] = Field(default=None, description="Status message")


class ReconciliationResponse(BaseModel):
    """Response schema for reconciliation."""

    date: str = Field(..., description="Reconciliation date")
    database_total_cents: int = Field(..., description="Total from database")
    database_count: int = Field(..., description="Count from database")
    stripe_total_cents: int = Field(..., description="Total from Stripe")
    stripe_count: int = Field(..., description="Count from Stripe")
    discrepancy_cents: int = Field(..., description="Amount discrepancy")
    discrepancy_count: int = Field(..., description="Count discrepancy")
    discrepancies: list[Dict[str, Any]] = Field(..., description="List of specific discrepancies")
