"""
Pytest configuration and fixtures.
"""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from api.main import app
from database.models import Base

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://postgres:postgres@localhost:5432/payments_test",
)


@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[AsyncSession, Any]:
    """
    Create test database session.

    Uses TEST_DATABASE_URL if provided so tests are not hardcoded to one local
    credential set.
    """
    engine = create_async_engine(
        TEST_DATABASE_URL,
        poolclass=NullPool,
    )

    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    session_factory = async_sessionmaker(
        engine,
        class_=AsyncSession,
        expire_on_commit=False,
    )

    async with session_factory() as session:
        yield session
        await session.rollback()

    await engine.dispose()


@pytest_asyncio.fixture
async def client() -> AsyncGenerator[AsyncClient, Any]:
    """Create test HTTP client."""
    async with AsyncClient(app=app, base_url="http://test") as ac:
        yield ac


@pytest.fixture
def sample_payment_data() -> dict[str, Any]:
    """Sample payment request data."""
    return {
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "amount_cents": 1000,
        "currency": "USD",
        "metadata": {"order_id": "test_order_123"},
    }