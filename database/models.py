"""SQLAlchemy database models for the payment processing system."""
from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


class Base(DeclarativeBase):
    """Base class for all database models."""


class Payment(Base):
    """
    Mutable payment state.

    This is the current view of a payment as used by the API and operational
    workflows.
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        primary_key=True,
        default=uuid.uuid4,
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255),
        unique=True,
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(
        String(255),
        unique=True,
        nullable=True,
        index=True,
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="pending",
        index=True,
    )
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(
        "metadata",
        JSONB,
        nullable=True,
    )
    response_snapshot: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        index=True,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
    )

    events: Mapped[list["PaymentEvent"]] = relationship(
        back_populates="payment",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="positive_amount"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'requires_payment_method', "
            "'requires_confirmation', 'requires_action', 'succeeded', 'failed', 'refunded')",
            name="valid_payment_status",
        ),
        CheckConstraint("length(currency) = 3", name="valid_currency"),
        Index("idx_payments_user_status", "user_id", "status"),
        Index("idx_payments_created_at", "created_at"),
    )

    def __repr__(self) -> str:
        return (
            f"<Payment(id={self.id}, user_id={self.user_id}, "
            f"amount_cents={self.amount_cents}, status={self.status})>"
        )


class PaymentEvent(Base):
    """
    Immutable audit history for payment lifecycle events.
    """

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("payments.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False, index=True)
    event_data: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        index=True,
    )

    payment: Mapped["Payment"] = relationship(back_populates="events")

    def __repr__(self) -> str:
        return (
            f"<PaymentEvent(id={self.id}, payment_id={self.payment_id}, "
            f"event_type={self.event_type})>"
        )


class ReconciliationStatus(Base):
    """
    Tracks the status and outcome of a reconciliation run for a specific date.
    """

    __tablename__ = "reconciliation_status"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reconciliation_date: Mapped[date] = mapped_column(Date, nullable=False, unique=True, index=True)
    stripe_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    database_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discrepancy_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discrepancy_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
    )
    completed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="valid_reconciliation_status",
        ),
        Index("idx_reconciliation_status_date", "reconciliation_date"),
        Index("idx_reconciliation_status_state", "status"),
    )

    def __repr__(self) -> str:
        return (
            f"<ReconciliationStatus(id={self.id}, "
            f"reconciliation_date={self.reconciliation_date}, status={self.status})>"
        )


class OutboxEvent(Base):
    """
    Transactional outbox table for deferred publishing.
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        nullable=False,
        index=True,
    )
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
    )
    published_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
    )

    __table_args__ = (
        Index("idx_outbox_published_created", "published", "created_at"),
        Index("idx_outbox_aggregate", "aggregate_id", "aggregate_type"),
    )

    def __repr__(self) -> str:
        return (
            f"<OutboxEvent(id={self.id}, event_type={self.event_type}, "
            f"published={self.published})>"
        )