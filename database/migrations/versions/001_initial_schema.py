"""Initial database schema

Revision ID: 001
Revises:
Create Date: 2025-01-06 00:00:00.000000

"""
from typing import Sequence, Union

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "001"
down_revision: Union[str, None] = None
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade database schema."""
    # Create payments table
    op.create_table(
        "payments",
        sa.Column("id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("idempotency_key", sa.String(length=255), nullable=False),
        sa.Column("user_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("stripe_payment_intent_id", sa.String(length=255), nullable=True),
        sa.Column("amount_cents", sa.Integer(), nullable=False),
        sa.Column("currency", sa.String(length=3), nullable=False),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("metadata", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("amount_cents > 0", name="positive_amount"),
        sa.CheckConstraint(
            "status IN ('pending', 'processing', 'succeeded', 'failed', 'refunded')",
            name="valid_status",
        ),
        sa.CheckConstraint("length(currency) = 3", name="valid_currency"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("idempotency_key"),
        sa.UniqueConstraint("stripe_payment_intent_id"),
    )
    op.create_index("idx_payments_created_desc", "payments", ["created_at"], unique=False)
    op.create_index("idx_payments_user_status", "payments", ["user_id", "status"], unique=False)
    op.create_index(
        op.f("ix_payments_created_at"), "payments", ["created_at"], unique=False
    )
    op.create_index(
        op.f("ix_payments_idempotency_key"), "payments", ["idempotency_key"], unique=False
    )
    op.create_index(op.f("ix_payments_status"), "payments", ["status"], unique=False)
    op.create_index(op.f("ix_payments_user_id"), "payments", ["user_id"], unique=False)

    # Create payment_events table
    op.create_table(
        "payment_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("payment_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("event_data", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("correlation_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_payment_events_correlation_id",
        "payment_events",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        "idx_payment_events_created_desc",
        "payment_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        "idx_payment_events_payment_id",
        "payment_events",
        ["payment_id"],
        unique=False,
    )
    op.create_index(
        "idx_payment_events_type",
        "payment_events",
        ["event_type"],
        unique=False,
    )
    op.create_index(
        op.f("ix_payment_events_correlation_id"),
        "payment_events",
        ["correlation_id"],
        unique=False,
    )
    op.create_index(
        op.f("ix_payment_events_created_at"),
        "payment_events",
        ["created_at"],
        unique=False,
    )
    op.create_index(
        op.f("ix_payment_events_payment_id"),
        "payment_events",
        ["payment_id"],
        unique=False,
    )

    # Create reconciliation_status table
    op.create_table(
        "reconciliation_status",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("reconciliation_date", sa.DateTime(timezone=False), nullable=False),
        sa.Column("stripe_total_cents", sa.BigInteger(), nullable=True),
        sa.Column("database_total_cents", sa.BigInteger(), nullable=True),
        sa.Column("discrepancy_cents", sa.BigInteger(), nullable=True),
        sa.Column("discrepancy_count", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(length=50), nullable=False),
        sa.Column("details", postgresql.JSONB(astext_type=sa.Text()), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('in_progress', 'completed', 'failed')",
            name="valid_reconciliation_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("reconciliation_date"),
    )
    op.create_index(
        "idx_reconciliation_date_desc",
        "reconciliation_status",
        ["reconciliation_date"],
        unique=False,
    )

    # Create outbox_events table
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.BigInteger(), autoincrement=True, nullable=False),
        sa.Column("aggregate_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column("aggregate_type", sa.String(length=100), nullable=False),
        sa.Column("event_type", sa.String(length=100), nullable=False),
        sa.Column("payload", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("published", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "idx_outbox_aggregate",
        "outbox_events",
        ["aggregate_id", "aggregate_type"],
        unique=False,
    )
    op.create_index(
        "idx_outbox_unpublished",
        "outbox_events",
        ["published", "created_at"],
        unique=False,
        postgresql_where=sa.text("NOT published"),
    )
    op.create_index(
        op.f("ix_outbox_events_published"),
        "outbox_events",
        ["published"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade database schema."""
    op.drop_index(op.f("ix_outbox_events_published"), table_name="outbox_events")
    op.drop_index(
        "idx_outbox_unpublished",
        table_name="outbox_events",
        postgresql_where=sa.text("NOT published"),
    )
    op.drop_index("idx_outbox_aggregate", table_name="outbox_events")
    op.drop_table("outbox_events")
    op.drop_index("idx_reconciliation_date_desc", table_name="reconciliation_status")
    op.drop_table("reconciliation_status")
    op.drop_index(op.f("ix_payment_events_payment_id"), table_name="payment_events")
    op.drop_index(op.f("ix_payment_events_created_at"), table_name="payment_events")
    op.drop_index(op.f("ix_payment_events_correlation_id"), table_name="payment_events")
    op.drop_index("idx_payment_events_type", table_name="payment_events")
    op.drop_index("idx_payment_events_payment_id", table_name="payment_events")
    op.drop_index("idx_payment_events_created_desc", table_name="payment_events")
    op.drop_index("idx_payment_events_correlation_id", table_name="payment_events")
    op.drop_table("payment_events")
    op.drop_index(op.f("ix_payments_user_id"), table_name="payments")
    op.drop_index(op.f("ix_payments_status"), table_name="payments")
    op.drop_index(op.f("ix_payments_idempotency_key"), table_name="payments")
    op.drop_index(op.f("ix_payments_created_at"), table_name="payments")
    op.drop_index("idx_payments_user_status", table_name="payments")
    op.drop_index("idx_payments_created_desc", table_name="payments")
    op.drop_table("payments")
