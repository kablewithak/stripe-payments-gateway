"""
Integration tests for health route contracts.

These tests patch the route-layer health_check singleton so health, liveness,
and readiness remain deterministic and do not depend on live DB/Redis/Stripe.
"""
from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest
from httpx import AsyncClient

from api import routes


class TestHealthRoutes:
    """Integration tests for health and probe endpoints."""

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_health_returns_200_with_healthy_payload(self, client: AsyncClient) -> None:
        """GET /health should return a healthy payload when checks succeed."""
        mocked_payload = {
            "status": "healthy",
            "checks": {
                "database": {
                    "status": "healthy",
                    "service": "database",
                    "message": "Database connection successful",
                },
                "redis": {
                    "status": "healthy",
                    "service": "redis",
                    "message": "Redis connection successful",
                },
                "stripe": {
                    "status": "healthy",
                    "service": "stripe",
                    "message": "Stripe API connection successful",
                    "test_mode": True,
                },
            },
        }

        with patch.object(
            routes.health_check,
            "check_all",
            AsyncMock(return_value=mocked_payload),
        ) as mock_check_all:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["checks"] == mocked_payload["checks"]
        assert data["message"] is None
        mock_check_all.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_health_returns_unhealthy_payload_when_check_all_raises(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /health should degrade to an unhealthy payload instead of raising 500."""
        with patch.object(
            routes.health_check,
            "check_all",
            AsyncMock(side_effect=RuntimeError("redis timeout")),
        ) as mock_check_all:
            response = await client.get("/health")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "unhealthy"
        assert data["checks"]["error"] == "redis timeout"
        mock_check_all.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_liveness_returns_200_with_alive_payload(self, client: AsyncClient) -> None:
        """GET /health/live should return the liveness payload."""
        mocked_payload = {
            "status": "alive",
            "message": "Application is running",
        }

        with patch.object(
            routes.health_check,
            "liveness",
            AsyncMock(return_value=mocked_payload),
        ) as mock_liveness:
            response = await client.get("/health/live")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "alive"
        assert data["message"] == "Application is running"
        assert data["checks"] is None
        mock_liveness.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_readiness_returns_200_when_healthy(self, client: AsyncClient) -> None:
        """GET /health/ready should return 200 when dependencies are healthy."""
        mocked_payload = {
            "status": "healthy",
            "checks": {
                "database": {"status": "healthy"},
                "redis": {"status": "healthy"},
                "stripe": {"status": "healthy"},
            },
        }

        with patch.object(
            routes.health_check,
            "readiness",
            AsyncMock(return_value=mocked_payload),
        ) as mock_readiness:
            response = await client.get("/health/ready")

        assert response.status_code == 200
        data = response.json()
        assert data["status"] == "healthy"
        assert data["checks"] == mocked_payload["checks"]
        assert data["message"] is None
        mock_readiness.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_readiness_returns_503_when_unhealthy(self, client: AsyncClient) -> None:
        """GET /health/ready should return 503 when readiness says unhealthy."""
        mocked_payload = {
            "status": "unhealthy",
            "checks": {
                "database": {"status": "healthy"},
                "redis": {"status": "unhealthy", "error": "Redis health check failed"},
                "stripe": {"status": "healthy"},
            },
        }

        with patch.object(
            routes.health_check,
            "readiness",
            AsyncMock(return_value=mocked_payload),
        ) as mock_readiness:
            response = await client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["detail"] == mocked_payload
        mock_readiness.assert_awaited_once()

    @pytest.mark.integration
    @pytest.mark.asyncio
    async def test_readiness_returns_503_with_fallback_payload_on_exception(
        self,
        client: AsyncClient,
    ) -> None:
        """GET /health/ready should return 503 with fallback detail when readiness raises."""
        with patch.object(
            routes.health_check,
            "readiness",
            AsyncMock(side_effect=RuntimeError("stripe timeout")),
        ) as mock_readiness:
            response = await client.get("/health/ready")

        assert response.status_code == 503
        assert response.json()["detail"] == {
            "status": "unhealthy",
            "error": "stripe timeout",
        }
        mock_readiness.assert_awaited_once()