"""
Outbox publisher background worker.

Continuously polls the outbox table and publishes events to message queue.
"""
import asyncio
import signal
import sys
from typing import Any, Dict

import structlog

from core.outbox import OutboxPublisher
from monitoring.logging import setup_logging

logger = structlog.get_logger(__name__)


async def publish_to_message_queue(event_data: Dict[str, Any]) -> None:
    """
    Publish event to message queue.

    Replace this with actual RabbitMQ/Redis Streams/Kafka publisher.

    Args:
        event_data: Event data to publish
    """
    # TODO: Implement actual message queue publishing
    # Example with RabbitMQ:
    # async with aio_pika.connect_robust(settings.rabbitmq_url) as connection:
    #     channel = await connection.channel()
    #     await channel.default_exchange.publish(
    #         aio_pika.Message(body=json.dumps(event_data).encode()),
    #         routing_key="payment_events"
    #     )

    logger.info(
        "event_published_to_queue",
        event_type=event_data.get("event_type"),
        aggregate_id=event_data.get("aggregate_id"),
    )


async def start_outbox_publisher() -> None:
    """
    Start the outbox publisher worker.

    Runs continuously until stopped.
    """
    setup_logging()

    logger.info("outbox_publisher_worker_starting")

    publisher = OutboxPublisher(
        publisher_func=publish_to_message_queue,
        batch_size=100,
        poll_interval_seconds=1.0,
    )

    # Setup signal handlers for graceful shutdown
    def signal_handler(sig: int, frame: Any) -> None:
        logger.info("outbox_publisher_worker_shutdown_signal_received", signal=sig)
        publisher.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    try:
        await publisher.start()
    except Exception as e:
        logger.error("outbox_publisher_worker_error", error=str(e))
        raise
    finally:
        logger.info("outbox_publisher_worker_stopped")


if __name__ == "__main__":
    asyncio.run(start_outbox_publisher())
