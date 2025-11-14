"""
Reconciliation engine for comparing Stripe reports with database records.

Runs daily to detect discrepancies such as:
- Missing payments in database
- Amount mismatches
- Status mismatches
"""
from datetime import date, datetime, timedelta
from typing import Any, Dict, List, Optional

import structlog
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_session_factory
from database.models import Payment, ReconciliationStatus
from integrations.stripe_client import StripeClient

logger = structlog.get_logger(__name__)


class ReconciliationError(Exception):
    """Raised when reconciliation fails."""

    pass


class ReconciliationEngine:
    """
    Reconciliation engine for daily payment verification.

    Compares Stripe PaymentIntents with database records to detect:
    - Missing payments
    - Amount discrepancies
    - Status mismatches
    """

    def __init__(self, stripe_client: Optional[StripeClient] = None):
        """
        Initialize reconciliation engine.

        Args:
            stripe_client: Optional Stripe client
        """
        self.stripe_client = stripe_client or StripeClient()
        logger.info("reconciliation_engine_initialized")

    async def _get_database_totals(
        self, db: AsyncSession, start_date: datetime, end_date: datetime
    ) -> Dict[str, Any]:
        """
        Get payment totals from database for date range.

        Args:
            db: Database session
            start_date: Start of date range
            end_date: End of date range

        Returns:
            Dict[str, Any]: Database totals
        """
        stmt = select(
            func.count(Payment.id).label("count"),
            func.sum(Payment.amount_cents).label("total_cents"),
        ).where(
            Payment.created_at >= start_date,
            Payment.created_at < end_date,
            Payment.status == "succeeded",
        )

        result = await db.execute(stmt)
        row = result.first()

        return {
            "count": row.count or 0,
            "total_cents": int(row.total_cents or 0),
        }

    async def _get_stripe_totals(
        self, start_timestamp: int, end_timestamp: int
    ) -> Dict[str, Any]:
        """
        Get payment totals from Stripe for date range.

        Args:
            start_timestamp: Start timestamp (Unix)
            end_timestamp: End timestamp (Unix)

        Returns:
            Dict[str, Any]: Stripe totals
        """
        total_cents = 0
        count = 0
        starting_after = None

        logger.info(
            "fetching_stripe_payments",
            start_timestamp=start_timestamp,
            end_timestamp=end_timestamp,
        )

        # Paginate through all payment intents
        while True:
            try:
                payment_intents = await self.stripe_client.list_payment_intents(
                    limit=100,
                    starting_after=starting_after,
                    created_gte=start_timestamp,
                    created_lte=end_timestamp,
                )

                for pi in payment_intents.data:
                    if pi.status == "succeeded":
                        total_cents += pi.amount
                        count += 1

                if not payment_intents.has_more:
                    break

                # Get last item for pagination
                if payment_intents.data:
                    starting_after = payment_intents.data[-1].id

            except Exception as e:
                logger.error("stripe_fetch_error", error=str(e))
                raise ReconciliationError(f"Failed to fetch Stripe data: {str(e)}")

        return {
            "count": count,
            "total_cents": total_cents,
        }

    async def _find_discrepancies(
        self, db: AsyncSession, start_date: datetime, end_date: datetime
    ) -> List[Dict[str, Any]]:
        """
        Find specific discrepancies between Stripe and database.

        Args:
            db: Database session
            start_date: Start of date range
            end_date: End of date range

        Returns:
            List[Dict[str, Any]]: List of discrepancies
        """
        discrepancies = []

        # Get all succeeded payments from database
        stmt = select(Payment).where(
            Payment.created_at >= start_date,
            Payment.created_at < end_date,
            Payment.status == "succeeded",
            Payment.stripe_payment_intent_id.isnot(None),
        )
        result = await db.execute(stmt)
        db_payments = {p.stripe_payment_intent_id: p for p in result.scalars().all()}

        # Get Stripe payment intents
        start_ts = int(start_date.timestamp())
        end_ts = int(end_date.timestamp())
        starting_after = None

        while True:
            payment_intents = await self.stripe_client.list_payment_intents(
                limit=100,
                starting_after=starting_after,
                created_gte=start_ts,
                created_lte=end_ts,
            )

            for pi in payment_intents.data:
                if pi.status != "succeeded":
                    continue

                db_payment = db_payments.get(pi.id)

                if db_payment is None:
                    # Payment in Stripe but not in database
                    discrepancies.append({
                        "type": "missing_in_database",
                        "payment_intent_id": pi.id,
                        "stripe_amount": pi.amount,
                        "stripe_currency": pi.currency,
                    })
                elif db_payment.amount_cents != pi.amount:
                    # Amount mismatch
                    discrepancies.append({
                        "type": "amount_mismatch",
                        "payment_id": str(db_payment.id),
                        "payment_intent_id": pi.id,
                        "database_amount": db_payment.amount_cents,
                        "stripe_amount": pi.amount,
                    })

            if not payment_intents.has_more:
                break

            if payment_intents.data:
                starting_after = payment_intents.data[-1].id

        return discrepancies

    async def reconcile_date(
        self, reconciliation_date: date
    ) -> Dict[str, Any]:
        """
        Reconcile payments for a specific date.

        Args:
            reconciliation_date: Date to reconcile

        Returns:
            Dict[str, Any]: Reconciliation results
        """
        logger.info(
            "reconciliation_started",
            date=reconciliation_date.isoformat(),
        )

        session_factory = get_session_factory()
        async with session_factory() as db:
            try:
                # Create reconciliation status record
                recon_status = ReconciliationStatus(
                    reconciliation_date=reconciliation_date,
                    status="in_progress",
                    started_at=datetime.utcnow(),
                )
                db.add(recon_status)
                await db.commit()

                # Define date range
                start_date = datetime.combine(reconciliation_date, datetime.min.time())
                end_date = start_date + timedelta(days=1)

                # Get database totals
                db_totals = await self._get_database_totals(db, start_date, end_date)

                # Get Stripe totals
                start_ts = int(start_date.timestamp())
                end_ts = int(end_date.timestamp())
                stripe_totals = await self._get_stripe_totals(start_ts, end_ts)

                # Calculate discrepancies
                discrepancy_cents = abs(
                    db_totals["total_cents"] - stripe_totals["total_cents"]
                )
                discrepancy_count = abs(db_totals["count"] - stripe_totals["count"])

                # Find specific discrepancies
                discrepancies = await self._find_discrepancies(db, start_date, end_date)

                # Update reconciliation status
                recon_status.stripe_total_cents = stripe_totals["total_cents"]
                recon_status.database_total_cents = db_totals["total_cents"]
                recon_status.discrepancy_cents = discrepancy_cents
                recon_status.discrepancy_count = discrepancy_count
                recon_status.status = "completed"
                recon_status.completed_at = datetime.utcnow()
                recon_status.details = {
                    "database": db_totals,
                    "stripe": stripe_totals,
                    "discrepancies": discrepancies[:100],  # Limit stored discrepancies
                }

                await db.commit()

                logger.info(
                    "reconciliation_completed",
                    date=reconciliation_date.isoformat(),
                    discrepancy_cents=discrepancy_cents,
                    discrepancy_count=discrepancy_count,
                    total_discrepancies=len(discrepancies),
                )

                return {
                    "date": reconciliation_date.isoformat(),
                    "database_total_cents": db_totals["total_cents"],
                    "database_count": db_totals["count"],
                    "stripe_total_cents": stripe_totals["total_cents"],
                    "stripe_count": stripe_totals["count"],
                    "discrepancy_cents": discrepancy_cents,
                    "discrepancy_count": discrepancy_count,
                    "discrepancies": discrepancies,
                }

            except Exception as e:
                logger.error(
                    "reconciliation_failed",
                    date=reconciliation_date.isoformat(),
                    error=str(e),
                )

                # Update status as failed
                if recon_status:
                    recon_status.status = "failed"
                    recon_status.completed_at = datetime.utcnow()
                    recon_status.details = {"error": str(e)}
                    await db.commit()

                raise ReconciliationError(f"Reconciliation failed: {str(e)}")

    async def reconcile_yesterday(self) -> Dict[str, Any]:
        """
        Reconcile payments for yesterday.

        Returns:
            Dict[str, Any]: Reconciliation results
        """
        yesterday = date.today() - timedelta(days=1)
        return await self.reconcile_date(yesterday)
