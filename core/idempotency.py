"""
Idempotency system for preventing duplicate payment charges.

This module implements a two-tier idempotency system:
1. Redis cache for fast lookups (primary)
2. Database fallback for persistence and reliability
"""
import hashlib
import uuid
from datetime import datetime
from typing import Any, Dict, Optional

import redis.asyncio as aioredis
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database.models import Payment

logger = structlog.get_logger(__name__)


class IdempotencyError(Exception):
    """Raised when idempotency validation fails."""

    pass


class IdempotencyManager:
    """
    Manages idempotency keys and cached responses.

    Implements a two-tier system:
    - Redis for fast cache lookups
    - PostgreSQL for durable storage
    """

    def __init__(self, redis_client: Optional[aioredis.Redis] = None):
        """
        Initialize idempotency manager.

        Args:
            redis_client: Optional Redis client (creates one if not provided)
        """
        self.settings = get_settings()
        self.redis_client = redis_client
        self._redis_initialized = False

    async def _ensure_redis(self) -> aioredis.Redis:
        """Ensure Redis client is initialized."""
        if self.redis_client is None or not self._redis_initialized:
            self.redis_client = await aioredis.from_url(
                self.settings.redis_url,
                encoding="utf-8",
                decode_responses=True,
            )
            self._redis_initialized = True
        return self.redis_client

    @staticmethod
    def generate_key(
        user_id: str | uuid.UUID,
        amount_cents: int,
        currency: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> str:
        """
        Generate idempotency key for a payment request.

        Format: {user_id}:{payment_hash}:{timestamp_hash}

        Args:
            user_id: User identifier
            amount_cents: Payment amount in cents
            currency: Currency code
            metadata: Optional payment metadata

        Returns:
            str: Idempotency key
        """
        user_id_str = str(user_id)

        # Create payment hash from amount and currency
        payment_data = f"{amount_cents}:{currency}"
        if metadata:
            # Sort metadata keys for consistent hashing
            sorted_metadata = ":".join(f"{k}={v}" for k, v in sorted(metadata.items()))
            payment_data += f":{sorted_metadata}"

        payment_hash = hashlib.sha256(payment_data.encode()).hexdigest()[:16]

        # Use timestamp for uniqueness
        timestamp = datetime.utcnow().isoformat()
        timestamp_hash = hashlib.md5(timestamp.encode()).hexdigest()[:8]

        return f"{user_id_str}:{payment_hash}:{timestamp_hash}"

    async def check_idempotency(
        self, idempotency_key: str, db: AsyncSession
    ) -> Optional[Dict[str, Any]]:
        """
        Check if a request with this idempotency key already exists.

        First checks Redis cache, then falls back to database.

        Args:
            idempotency_key: The idempotency key to check
            db: Database session

        Returns:
            Optional[Dict[str, Any]]: Cached payment response if exists, None otherwise
        """
        logger.info("checking_idempotency", idempotency_key=idempotency_key)

        # Check Redis cache first
        try:
            redis = await self._ensure_redis()
            cached_response = await redis.get(f"idempotency:{idempotency_key}")
            if cached_response:
                logger.info(
                    "idempotency_cache_hit",
                    idempotency_key=idempotency_key,
                    source="redis",
                )
                import json

                return json.loads(cached_response)
        except Exception as e:
            logger.warning(
                "redis_cache_error",
                error=str(e),
                idempotency_key=idempotency_key,
            )

        # Fallback to database
        try:
            stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
            result = await db.execute(stmt)
            payment = result.scalar_one_or_none()

            if payment:
                logger.info(
                    "idempotency_cache_hit",
                    idempotency_key=idempotency_key,
                    source="database",
                )
                payment_dict = {
                    "id": str(payment.id),
                    "user_id": str(payment.user_id),
                    "amount_cents": payment.amount_cents,
                    "currency": payment.currency,
                    "status": payment.status,
                    "stripe_payment_intent_id": payment.stripe_payment_intent_id,
                    "created_at": payment.created_at.isoformat(),
                }

                # Cache in Redis for future requests
                try:
                    redis = await self._ensure_redis()
                    import json

                    await redis.setex(
                        f"idempotency:{idempotency_key}",
                        self.settings.idempotency_cache_ttl,
                        json.dumps(payment_dict),
                    )
                except Exception as e:
                    logger.warning("redis_cache_set_error", error=str(e))

                return payment_dict

        except Exception as e:
            logger.error(
                "database_idempotency_check_error",
                error=str(e),
                idempotency_key=idempotency_key,
            )
            raise IdempotencyError(f"Failed to check idempotency: {str(e)}")

        logger.info("idempotency_cache_miss", idempotency_key=idempotency_key)
        return None

    async def store_response(
        self, idempotency_key: str, response: Dict[str, Any]
    ) -> None:
        """
        Store payment response in cache for idempotency.

        Args:
            idempotency_key: The idempotency key
            response: Payment response to cache
        """
        try:
            redis = await self._ensure_redis()
            import json

            await redis.setex(
                f"idempotency:{idempotency_key}",
                self.settings.idempotency_cache_ttl,
                json.dumps(response),
            )
            logger.info("idempotency_response_cached", idempotency_key=idempotency_key)
        except Exception as e:
            logger.warning(
                "idempotency_cache_store_error",
                error=str(e),
                idempotency_key=idempotency_key,
            )

    async def invalidate(self, idempotency_key: str) -> None:
        """
        Invalidate cached response for an idempotency key.

        Args:
            idempotency_key: The idempotency key to invalidate
        """
        try:
            redis = await self._ensure_redis()
            await redis.delete(f"idempotency:{idempotency_key}")
            logger.info("idempotency_cache_invalidated", idempotency_key=idempotency_key)
        except Exception as e:
            logger.warning(
                "idempotency_cache_invalidate_error",
                error=str(e),
                idempotency_key=idempotency_key,
            )

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis_client and self._redis_initialized:
            await self.redis_client.close()
