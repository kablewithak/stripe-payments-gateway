"""
Idempotency system for preventing duplicate payment charges.

This module implements a two-tier idempotency system:
1. Redis cache for fast lookups (primary)
2. Database fallback for persistence and reliability
"""
from __future__ import annotations

import hashlib
import json
import uuid
from typing import Any

import redis.asyncio as aioredis
import structlog
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from config import get_settings
from database.models import Payment

logger = structlog.get_logger(__name__)


class IdempotencyError(Exception):
    """Raised when idempotency validation fails."""


class IdempotencyManager:
    """
    Manages idempotency keys and cached responses.

    Implements a two-tier system:
    - Redis for fast cache lookups
    - PostgreSQL for durable storage
    """

    def __init__(self, redis_client: aioredis.Redis | None = None) -> None:
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
    def _canonicalize_metadata(metadata: dict[str, Any] | None) -> str:
        """
        Canonicalize metadata for deterministic hashing.

        Keys are sorted and JSON is serialized without extra whitespace so the
        same logical metadata always produces the same byte representation.
        """
        if not metadata:
            return ""

        return json.dumps(
            metadata,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )

    @classmethod
    def generate_key(
        cls,
        user_id: str | uuid.UUID,
        amount_cents: int,
        currency: str,
        metadata: dict[str, Any] | None = None,
    ) -> str:
        """
        Generate a deterministic idempotency key for a payment request.

        Format:
            {user_id}:{request_hash}

        The same logical request must always produce the same key. Time-based
        entropy is deliberately excluded.
        """
        user_id_str = str(user_id)
        normalized_currency = currency.upper()
        canonical_metadata = cls._canonicalize_metadata(metadata)

        request_fingerprint = {
            "amount_cents": amount_cents,
            "currency": normalized_currency,
            "metadata": canonical_metadata,
        }
        payload = json.dumps(
            request_fingerprint,
            sort_keys=True,
            separators=(",", ":"),
            ensure_ascii=False,
        )
        request_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()[:24]

        return f"{user_id_str}:{request_hash}"

    @staticmethod
    def _build_payment_response(payment: Payment) -> dict[str, Any]:
        """Build the canonical cached response shape from a Payment model."""
        return {
            "id": str(payment.id),
            "user_id": str(payment.user_id),
            "amount_cents": payment.amount_cents,
            "currency": payment.currency,
            "status": payment.status,
            "stripe_payment_intent_id": payment.stripe_payment_intent_id,
            "idempotency_key": payment.idempotency_key,
            "created_at": payment.created_at.isoformat(),
        }

    async def check_idempotency(
        self,
        idempotency_key: str,
        db: AsyncSession,
    ) -> dict[str, Any] | None:
        """
        Check if a request with this idempotency key already exists.

        First checks Redis cache, then falls back to database.
        """
        logger.info("checking_idempotency", idempotency_key=idempotency_key)

        try:
            redis = await self._ensure_redis()
            cached_response = await redis.get(f"idempotency:{idempotency_key}")
            if cached_response:
                logger.info(
                    "idempotency_cache_hit",
                    idempotency_key=idempotency_key,
                    source="redis",
                )
                return json.loads(cached_response)
        except Exception as exc:
            logger.warning(
                "redis_cache_error",
                error=str(exc),
                idempotency_key=idempotency_key,
            )

        try:
            stmt = select(Payment).where(Payment.idempotency_key == idempotency_key)
            result = await db.execute(stmt)
            payment = result.scalar_one_or_none()

            if payment is None:
                logger.info("idempotency_cache_miss", idempotency_key=idempotency_key)
                return None

            logger.info(
                "idempotency_cache_hit",
                idempotency_key=idempotency_key,
                source="database",
            )
            payment_dict = self._build_payment_response(payment)

            try:
                redis = await self._ensure_redis()
                await redis.setex(
                    f"idempotency:{idempotency_key}",
                    self.settings.idempotency_cache_ttl,
                    json.dumps(payment_dict),
                )
            except Exception as exc:
                logger.warning("redis_cache_set_error", error=str(exc))

            return payment_dict

        except Exception as exc:
            logger.error(
                "database_idempotency_check_error",
                error=str(exc),
                idempotency_key=idempotency_key,
            )
            raise IdempotencyError(f"Failed to check idempotency: {exc}") from exc

    async def store_response(self, idempotency_key: str, response: dict[str, Any]) -> None:
        """Store payment response in cache for idempotency."""
        try:
            redis = await self._ensure_redis()
            await redis.setex(
                f"idempotency:{idempotency_key}",
                self.settings.idempotency_cache_ttl,
                json.dumps(response),
            )
            logger.info("idempotency_response_cached", idempotency_key=idempotency_key)
        except Exception as exc:
            logger.warning(
                "idempotency_cache_store_error",
                error=str(exc),
                idempotency_key=idempotency_key,
            )

    async def invalidate(self, idempotency_key: str) -> None:
        """Invalidate cached response for an idempotency key."""
        try:
            redis = await self._ensure_redis()
            await redis.delete(f"idempotency:{idempotency_key}")
            logger.info("idempotency_cache_invalidated", idempotency_key=idempotency_key)
        except Exception as exc:
            logger.warning(
                "idempotency_cache_invalidate_error",
                error=str(exc),
                idempotency_key=idempotency_key,
            )

    async def close(self) -> None:
        """Close Redis connection."""
        if self.redis_client and self._redis_initialized:
            await self.redis_client.close()