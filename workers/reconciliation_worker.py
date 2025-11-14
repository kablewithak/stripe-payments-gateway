"""
Reconciliation background worker.

Runs daily reconciliation at scheduled time (e.g., 2 AM).
"""
import asyncio
import signal
import sys
from datetime import datetime, timedelta

import structlog

from core.reconciliation import ReconciliationEngine
from monitoring.logging import setup_logging

logger = structlog.get_logger(__name__)


async def run_daily_reconciliation() -> None:
    """
    Run daily reconciliation for yesterday's payments.
    """
    logger.info("daily_reconciliation_started")

    try:
        engine = ReconciliationEngine()

        # Reconcile yesterday
        result = await engine.reconcile_yesterday()

        logger.info(
            "daily_reconciliation_completed",
            date=result["date"],
            discrepancy_cents=result["discrepancy_cents"],
            discrepancy_count=result["discrepancy_count"],
            total_discrepancies=len(result["discrepancies"]),
        )

        # Alert if discrepancies found
        if result["discrepancy_cents"] > 0 or result["discrepancy_count"] > 0:
            logger.warning(
                "reconciliation_discrepancies_detected",
                date=result["date"],
                discrepancy_cents=result["discrepancy_cents"],
                discrepancy_count=result["discrepancy_count"],
            )
            # TODO: Send alert (email, Slack, PagerDuty, etc.)

    except Exception as e:
        logger.error("daily_reconciliation_failed", error=str(e))
        # TODO: Send critical alert
        raise


async def calculate_next_run_time(target_hour: int = 2) -> float:
    """
    Calculate seconds until next scheduled run.

    Args:
        target_hour: Hour of day to run (24-hour format)

    Returns:
        float: Seconds until next run
    """
    now = datetime.now()
    next_run = now.replace(hour=target_hour, minute=0, second=0, microsecond=0)

    # If we've passed today's run time, schedule for tomorrow
    if now >= next_run:
        next_run += timedelta(days=1)

    seconds_until = (next_run - now).total_seconds()

    logger.info(
        "reconciliation_next_run_scheduled",
        next_run=next_run.isoformat(),
        seconds_until=seconds_until,
    )

    return seconds_until


async def start_reconciliation_worker(target_hour: int = 2) -> None:
    """
    Start the reconciliation worker.

    Runs daily at specified hour.

    Args:
        target_hour: Hour of day to run (default: 2 AM)
    """
    setup_logging()

    logger.info("reconciliation_worker_starting", target_hour=target_hour)

    running = True

    def signal_handler(sig: int, frame: Any) -> None:
        nonlocal running
        logger.info("reconciliation_worker_shutdown_signal_received", signal=sig)
        running = False

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        while running:
            # Calculate next run time
            seconds_until = await calculate_next_run_time(target_hour)

            # Wait until next run time (with periodic checks for shutdown signal)
            while seconds_until > 0 and running:
                sleep_time = min(seconds_until, 60)  # Check every minute
                await asyncio.sleep(sleep_time)
                seconds_until -= sleep_time

            if not running:
                break

            # Run reconciliation
            try:
                await run_daily_reconciliation()
            except Exception as e:
                logger.error("reconciliation_execution_error", error=str(e))
                # Continue running even if one reconciliation fails

    except Exception as e:
        logger.error("reconciliation_worker_error", error=str(e))
        raise
    finally:
        logger.info("reconciliation_worker_stopped")


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="Reconciliation worker")
    parser.add_argument(
        "--hour", type=int, default=2, help="Hour of day to run reconciliation (0-23)"
    )
    args = parser.parse_args()

    asyncio.run(start_reconciliation_worker(target_hour=args.hour))
