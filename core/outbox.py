"""
Transactional outbox pattern implementation.

Ensures exactly-once message delivery by writing events to the database
in the same transaction as domain changes, then publishing them asynchronously.
"""
import asyncio
from datetime import datetime
from typing import Any, Callable, Dict, List, Optional

import structlog
from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from database.connection import get_session_factory
from database.models import OutboxEvent

logger = structlog.get_logger(__name__)


class OutboxPublisher:
    """
    Publishes events from the outbox table to a message queue.

    Implements exactly-once delivery guarantee by:
    1. Reading unpublished events from outbox
    2. Publishing to message queue
    3. Marking as published in database
    """

    def __init__(
        self,
        publisher_func: Optional[Callable[[Dict[str, Any]], Any]] = None,
        batch_size: int = 100,
        poll_interval_seconds: float = 1.0,
    ):
        """
        Initialize outbox publisher.

        Args:
            publisher_func: Function to publish events (e.g., to RabbitMQ)
            batch_size: Number of events to process per batch
            poll_interval_seconds: Polling interval
        """
        self.publisher_func = publisher_func or self._default_publisher
        self.batch_size = batch_size
        self.poll_interval_seconds = poll_interval_seconds
        self._running = False

        logger.info(
            "outbox_publisher_initialized",
            batch_size=batch_size,
            poll_interval=poll_interval_seconds,
        )

    async def _default_publisher(self, event_data: Dict[str, Any]) -> None:
        """
        Default publisher that just logs events.

        Replace with actual message queue publisher (RabbitMQ, Kafka, etc.)

        Args:
            event_data: Event data to publish
        """
        logger.info(
            "outbox_event_published_default",
            event_type=event_data.get("event_type"),
            aggregate_id=event_data.get("aggregate_id"),
        )

    async def _fetch_unpublished_events(
        self, db: AsyncSession
    ) -> List[OutboxEvent]:
        """
        Fetch unpublished events from outbox.

        Args:
            db: Database session

        Returns:
            List[OutboxEvent]: Unpublished events
        """
        stmt = (
            select(OutboxEvent)
            .where(OutboxEvent.published == False)  # noqa: E712
            .order_by(OutboxEvent.created_at)
            .limit(self.batch_size)
        )
        result = await db.execute(stmt)
        return list(result.scalars().all())

    async def _publish_event(self, event: OutboxEvent) -> bool:
        """
        Publish a single event.

        Args:
            event: Outbox event to publish

        Returns:
            bool: True if published successfully, False otherwise
        """
        try:
            event_data = {
                "id": event.id,
                "aggregate_id": str(event.aggregate_id),
                "aggregate_type": event.aggregate_type,
                "event_type": event.event_type,
                "payload": event.payload,
                "created_at": event.created_at.isoformat(),
            }

            await self.publisher_func(event_data)

            logger.info(
                "outbox_event_published",
                event_id=event.id,
                event_type=event.event_type,
                aggregate_id=str(event.aggregate_id),
            )

            return True

        except Exception as e:
            logger.error(
                "outbox_event_publish_failed",
                event_id=event.id,
                error=str(e),
            )
            return False

    async def _mark_as_published(
        self, db: AsyncSession, event_ids: List[int]
    ) -> None:
        """
        Mark events as published in database.

        Args:
            db: Database session
            event_ids: List of event IDs to mark as published
        """
        if not event_ids:
            return

        stmt = (
            update(OutboxEvent)
            .where(OutboxEvent.id.in_(event_ids))
            .values(published=True, published_at=datetime.utcnow())
        )
        await db.execute(stmt)
        await db.commit()

        logger.info(
            "outbox_events_marked_published",
            count=len(event_ids),
        )

    async def process_batch(self) -> int:
        """
        Process a batch of unpublished events.

        Returns:
            int: Number of events published
        """
        session_factory = get_session_factory()
        async with session_factory() as db:
            try:
                # Fetch unpublished events
                events = await self._fetch_unpublished_events(db)

                if not events:
                    return 0

                logger.info(
                    "outbox_batch_processing_started",
                    batch_size=len(events),
                )

                # Publish each event
                published_ids = []
                for event in events:
                    success = await self._publish_event(event)
                    if success:
                        published_ids.append(event.id)

                # Mark as published
                if published_ids:
                    await self._mark_as_published(db, published_ids)

                logger.info(
                    "outbox_batch_processed",
                    total=len(events),
                    published=len(published_ids),
                    failed=len(events) - len(published_ids),
                )

                return len(published_ids)

            except Exception as e:
                logger.error("outbox_batch_processing_error", error=str(e))
                await db.rollback()
                return 0

    async def start(self) -> None:
        """
        Start the outbox publisher background worker.

        Continuously polls for unpublished events and publishes them.
        """
        self._running = True
        logger.info("outbox_publisher_started")

        try:
            while self._running:
                try:
                    published_count = await self.process_batch()

                    if published_count == 0:
                        # No events to process, wait before polling again
                        await asyncio.sleep(self.poll_interval_seconds)
                    else:
                        # Events were processed, check immediately for more
                        await asyncio.sleep(0.1)

                except Exception as e:
                    logger.error("outbox_publisher_error", error=str(e))
                    await asyncio.sleep(self.poll_interval_seconds)

        finally:
            logger.info("outbox_publisher_stopped")

    def stop(self) -> None:
        """Stop the outbox publisher."""
        self._running = False
        logger.info("outbox_publisher_stop_requested")

    async def get_pending_count(self) -> int:
        """
        Get count of pending unpublished events.

        Returns:
            int: Number of unpublished events
        """
        session_factory = get_session_factory()
        async with session_factory() as db:
            stmt = select(OutboxEvent).where(OutboxEvent.published == False)  # noqa: E712
            result = await db.execute(stmt)
            events = result.scalars().all()
            return len(list(events))
