"""SQLAlchemy database models for payment processing system."""
import uuid
from datetime import datetime
from typing import Any, Dict

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    Index,
    Integer,
    String,
    Text,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Base class for all database models."""

    pass


class Payment(Base):
    """
    Payment records table.

    Stores all payment transactions with their current status and metadata.
    Includes idempotency key for preventing duplicate charges.
    """

    __tablename__ = "payments"

    id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), primary_key=True, default=uuid.uuid4
    )
    idempotency_key: Mapped[str] = mapped_column(
        String(255), unique=True, nullable=False, index=True
    )
    user_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False, index=True)
    stripe_payment_intent_id: Mapped[str | None] = mapped_column(
        String(255), unique=True, nullable=True
    )
    amount_cents: Mapped[int] = mapped_column(Integer, nullable=False)
    currency: Mapped[str] = mapped_column(String(3), nullable=False, default="USD")
    status: Mapped[str] = mapped_column(String(50), nullable=False, index=True)
    metadata: Mapped[Dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), index=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        default=func.now(),
        onupdate=func.now(),
    )

    __table_args__ = (
        CheckConstraint("amount_cents > 0", name="positive_amount"),
        CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'refunded')",
            name="valid_status",
        ),
        CheckConstraint("length(currency) = 3", name="valid_currency"),
        Index("idx_payments_user_status", "user_id", "status"),
        Index("idx_payments_created_desc", "created_at", postgresql_ops={"created_at": "DESC"}),
    )

    def __repr__(self) -> str:
        """String representation of Payment."""
        return (
            f"<Payment(id={self.id}, user_id={self.user_id}, "
            f"amount={self.amount_cents}, status={self.status})>"
        )


class PaymentEvent(Base):
    """
    Payment events audit trail table.

    Stores all events related to a payment for complete audit trail.
    Immutable once written.
    """

    __tablename__ = "payment_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    payment_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_data: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    correlation_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now(), index=True
    )

    __table_args__ = (
        Index("idx_payment_events_payment_id", "payment_id"),
        Index("idx_payment_events_correlation_id", "correlation_id"),
        Index(
            "idx_payment_events_created_desc",
            "created_at",
            postgresql_ops={"created_at": "DESC"},
        ),
        Index("idx_payment_events_type", "event_type"),
    )

    def __repr__(self) -> str:
        """String representation of PaymentEvent."""
        return (
            f"<PaymentEvent(id={self.id}, payment_id={self.payment_id}, "
            f"type={self.event_type})>"
        )


class ReconciliationStatus(Base):
    """
    Daily reconciliation status tracking table.

    Stores the results of daily reconciliation jobs comparing
    Stripe reports with database records.
    """

    __tablename__ = "reconciliation_status"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    reconciliation_date: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), nullable=False, unique=True
    )
    stripe_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    database_total_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discrepancy_cents: Mapped[int | None] = mapped_column(BigInteger, nullable=True)
    discrepancy_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    status: Mapped[str] = mapped_column(String(50), nullable=False)
    details: Mapped[Dict[str, Any] | None] = mapped_column(JSONB, nullable=True)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="valid_reconciliation_status",
        ),
        Index(
            "idx_reconciliation_date_desc",
            "reconciliation_date",
            postgresql_ops={"reconciliation_date": "DESC"},
        ),
    )

    def __repr__(self) -> str:
        """String representation of ReconciliationStatus."""
        return (
            f"<ReconciliationStatus(id={self.id}, date={self.reconciliation_date}, "
            f"status={self.status})>"
        )


class OutboxEvent(Base):
    """
    Transactional outbox events table.

    Implements the transactional outbox pattern for exactly-once message delivery.
    Events are written in the same transaction as domain changes,
    then published asynchronously by a background worker.
    """

    __tablename__ = "outbox_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    aggregate_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    aggregate_type: Mapped[str] = mapped_column(String(100), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[Dict[str, Any]] = mapped_column(JSONB, nullable=False)
    published: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    __table_args__ = (
        Index(
            "idx_outbox_unpublished",
            "published",
            "created_at",
            postgresql_where=Text("NOT published"),
        ),
        Index("idx_outbox_aggregate", "aggregate_id", "aggregate_type"),
    )

    def __repr__(self) -> str:
        """String representation of OutboxEvent."""
        return (
            f"<OutboxEvent(id={self.id}, type={self.event_type}, "
            f"published={self.published})>"
        )
