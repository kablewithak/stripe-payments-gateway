"""
Health check endpoints for Kubernetes readiness/liveness probes.

Checks:
- Database connectivity
- Redis connectivity
- Stripe API reachability
"""
from typing import Any, Dict

import redis.asyncio as aioredis
import stripe
import structlog
from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database.connection import get_session_factory

logger = structlog.get_logger(__name__)


class HealthCheckError(Exception):
    """Raised when health check fails."""

    pass


class HealthCheck:
    """
    Health check service for monitoring system dependencies.

    Provides:
    - Database connectivity check
    - Redis connectivity check
    - Stripe API reachability check
    - Overall system health status
    """

    def __init__(self) -> None:
        """Initialize health check service."""
        self.settings = get_settings()

    async def check_database(self) -> Dict[str, Any]:
        """
        Check database connectivity.

        Returns:
            Dict[str, Any]: Database health status

        Raises:
            HealthCheckError: If database check fails
        """
        try:
            session_factory = get_session_factory()
            async with session_factory() as db:
                # Simple query to check connectivity
                result = await db.execute(text("SELECT 1"))
                result.scalar()

                return {
                    "status": "healthy",
                    "service": "database",
                    "message": "Database connection successful",
                }

        except Exception as e:
            logger.error("database_health_check_failed", error=str(e))
            raise HealthCheckError(f"Database health check failed: {str(e)}")

    async def check_redis(self) -> Dict[str, Any]:
        """
        Check Redis connectivity.

        Returns:
            Dict[str, Any]: Redis health status

        Raises:
            HealthCheckError: If Redis check fails
        """
        redis_client: aioredis.Redis | None = None
        try:
            redis_client = await aioredis.from_url(
                self.settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )

            # Simple ping to check connectivity
            await redis_client.ping()

            return {
                "status": "healthy",
                "service": "redis",
                "message": "Redis connection successful",
            }

        except Exception as e:
            logger.error("redis_health_check_failed", error=str(e))
            raise HealthCheckError(f"Redis health check failed: {str(e)}")

        finally:
            if redis_client:
                await redis_client.close()

    async def check_stripe(self) -> Dict[str, Any]:
        """
        Check Stripe API reachability.

        Returns:
            Dict[str, Any]: Stripe health status

        Raises:
            HealthCheckError: If Stripe check fails
        """
        try:
            # Set Stripe API key
            stripe.api_key = self.settings.stripe_secret_key

            # Simple API call to check connectivity
            # List payment methods with limit 1 (minimal data transfer)
            await asyncio.get_event_loop().run_in_executor(
                None, lambda: stripe.PaymentMethod.list(limit=1)
            )

            return {
                "status": "healthy",
                "service": "stripe",
                "message": "Stripe API connection successful",
                "test_mode": self.settings.is_test_mode,
            }

        except Exception as e:
            logger.error("stripe_health_check_failed", error=str(e))
            raise HealthCheckError(f"Stripe health check failed: {str(e)}")

    async def check_all(self) -> Dict[str, Any]:
        """
        Run all health checks.

        Returns:
            Dict[str, Any]: Overall health status
        """
        checks = {}
        all_healthy = True

        # Database check
        try:
            checks["database"] = await self.check_database()
        except HealthCheckError as e:
            checks["database"] = {
                "status": "unhealthy",
                "service": "database",
                "error": str(e),
            }
            all_healthy = False

        # Redis check
        try:
            checks["redis"] = await self.check_redis()
        except HealthCheckError as e:
            checks["redis"] = {
                "status": "unhealthy",
                "service": "redis",
                "error": str(e),
            }
            all_healthy = False

        # Stripe check
        try:
            checks["stripe"] = await self.check_stripe()
        except HealthCheckError as e:
            checks["stripe"] = {
                "status": "unhealthy",
                "service": "stripe",
                "error": str(e),
            }
            all_healthy = False

        return {
            "status": "healthy" if all_healthy else "unhealthy",
            "checks": checks,
        }

    async def liveness(self) -> Dict[str, Any]:
        """
        Liveness probe endpoint.

        Simple check that the application is running.
        Does not check external dependencies.

        Returns:
            Dict[str, Any]: Liveness status
        """
        return {
            "status": "alive",
            "message": "Application is running",
        }

    async def readiness(self) -> Dict[str, Any]:
        """
        Readiness probe endpoint.

        Checks if application is ready to accept traffic.
        Verifies all dependencies are available.

        Returns:
            Dict[str, Any]: Readiness status
        """
        return await self.check_all()


# Import asyncio at module level
import asyncio
