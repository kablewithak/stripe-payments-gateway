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

TEST_DATABASE_URL = os.getenv(
    "TEST_DATABASE_URL",
    "postgresql+asyncpg://payments_test_user:payments_test_pw@localhost:5432/payments_test",
)

# Make test runtime deterministic before importing the app.
os.environ.setdefault("DATABASE_URL", TEST_DATABASE_URL)

from api.main import app  # noqa: E402
from database.models import Base  # noqa: E402


@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[AsyncSession, Any]:
    """
    Create a clean test database session.

    Uses TEST_DATABASE_URL so tests never depend on the app runtime DB.
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