"""
Structured logging configuration.

Uses structlog for JSON-formatted logs with correlation IDs.
"""
import logging
import sys
from typing import Any

import structlog
from pythonjsonlogger import jsonlogger

from config import get_settings


def add_app_context(
    logger: Any, method_name: str, event_dict: dict[str, Any]
) -> dict[str, Any]:
    """
    Add application context to log events.

    Args:
        logger: Logger instance
        method_name: Log method name
        event_dict: Event dictionary

    Returns:
        dict[str, Any]: Enhanced event dictionary
    """
    settings = get_settings()
    event_dict["app_name"] = settings.app_name
    event_dict["app_env"] = settings.app_env
    return event_dict


def setup_logging() -> None:
    """
    Configure structured logging with JSON formatter.

    Sets up:
    - JSON-formatted logs
    - Correlation ID tracking
    - Structured log fields
    - ELK/Loki compatibility
    """
    settings = get_settings()

    # Configure structlog
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            structlog.stdlib.filter_by_level,
            structlog.stdlib.add_logger_name,
            structlog.stdlib.add_log_level,
            structlog.stdlib.PositionalArgumentsFormatter(),
            structlog.processors.TimeStamper(fmt="iso"),
            structlog.processors.StackInfoRenderer(),
            structlog.processors.format_exc_info,
            structlog.processors.UnicodeDecoder(),
            add_app_context,
            structlog.processors.JSONRenderer(),
        ],
        wrapper_class=structlog.stdlib.BoundLogger,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Configure standard library logging
    root_logger = logging.getLogger()
    root_logger.setLevel(getattr(logging, settings.log_level))

    # Remove existing handlers
    for handler in root_logger.handlers[:]:
        root_logger.removeHandler(handler)

    # Add JSON handler for stdout
    json_handler = logging.StreamHandler(sys.stdout)
    formatter = jsonlogger.JsonFormatter(
        "%(timestamp)s %(level)s %(name)s %(message)s",
        rename_fields={
            "timestamp": "@timestamp",
            "level": "level",
            "name": "logger",
            "message": "message",
        },
    )
    json_handler.setFormatter(formatter)
    root_logger.addHandler(json_handler)

    # Suppress noisy loggers
    logging.getLogger("urllib3").setLevel(logging.WARNING)
    logging.getLogger("stripe").setLevel(logging.INFO)

    logger = structlog.get_logger(__name__)
    logger.info(
        "logging_configured",
        log_level=settings.log_level,
        app_env=settings.app_env,
    )


def get_logger(name: str) -> Any:
    """
    Get a structured logger instance.

    Args:
        name: Logger name

    Returns:
        Any: Structured logger
    """
    return structlog.get_logger(name)
