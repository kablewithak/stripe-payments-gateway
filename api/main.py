"""
Main FastAPI application.

Production-grade payment processing API with:
- CORS configuration
- Error handling
- Request ID tracking
- Structured logging
- Prometheus metrics
"""
import time
import uuid
from contextlib import asynccontextmanager
from typing import Any, AsyncGenerator

import structlog
from fastapi import FastAPI, Request, Response, status
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from config import get_settings
from database.connection import close_db, init_db
from monitoring.logging import setup_logging

from .routes import admin_router, monitoring_router, payment_router, webhook_router

# Setup logging first
setup_logging()
logger = structlog.get_logger(__name__)

settings = get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncGenerator[None, Any]:
    """
    Application lifespan manager.

    Handles startup and shutdown events.
    """
    # Startup
    logger.info(
        "application_startup",
        app_name=settings.app_name,
        env=settings.app_env,
        test_mode=settings.is_test_mode,
    )

    # Initialize database
    try:
        await init_db()
        logger.info("database_initialized")
    except Exception as e:
        logger.error("database_initialization_failed", error=str(e))
        raise

    yield

    # Shutdown
    logger.info("application_shutdown")
    try:
        await close_db()
        logger.info("database_connections_closed")
    except Exception as e:
        logger.error("database_shutdown_error", error=str(e))


# Create FastAPI application
app = FastAPI(
    title="Payment Processing System",
    description=(
        "Production-grade distributed payment processing system with Stripe integration. "
        "Features: idempotency, distributed locking, webhook handling, reconciliation, "
        "and comprehensive monitoring."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs",
    redoc_url="/redoc",
    openapi_url="/openapi.json",
)

# CORS configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=settings.get_allowed_origins_list(),
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.middleware("http")
async def add_request_id_middleware(request: Request, call_next: Any) -> Response:
    """
    Add request ID to all requests for tracing.

    Also adds timing information and structured logging context.
    """
    request_id = str(uuid.uuid4())
    start_time = time.time()

    # Add to structlog context
    structlog.contextvars.bind_contextvars(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
    )

    logger.info(
        "request_started",
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_host=request.client.host if request.client else None,
    )

    try:
        response = await call_next(request)

        # Add request ID to response headers
        response.headers["X-Request-ID"] = request_id

        # Log request completion
        duration = time.time() - start_time
        logger.info(
            "request_completed",
            request_id=request_id,
            status_code=response.status_code,
            duration_seconds=duration,
        )

        return response

    except Exception as e:
        duration = time.time() - start_time
        logger.error(
            "request_failed",
            request_id=request_id,
            error=str(e),
            duration_seconds=duration,
        )
        raise

    finally:
        # Clear structlog context
        structlog.contextvars.clear_contextvars()


@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    """
    Global exception handler for unhandled exceptions.
    """
    logger.error(
        "unhandled_exception",
        error=str(exc),
        error_type=type(exc).__name__,
        path=request.url.path,
    )

    return JSONResponse(
        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
        content={
            "error": "Internal server error",
            "message": "An unexpected error occurred. Please try again later.",
        },
    )


# Include routers
app.include_router(payment_router)
app.include_router(webhook_router)
app.include_router(admin_router)
app.include_router(monitoring_router)


@app.get("/", tags=["root"])
async def root() -> dict[str, Any]:
    """Root endpoint with API information."""
    return {
        "service": "payment-systems",
        "version": "1.0.0",
        "status": "operational",
        "environment": settings.app_env,
        "test_mode": settings.is_test_mode,
        "docs": "/docs",
        "health": "/health",
        "metrics": "/metrics",
    }


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        "api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        reload=settings.debug,
        workers=settings.api_workers if not settings.debug else 1,
        log_level=settings.log_level.lower(),
    )
