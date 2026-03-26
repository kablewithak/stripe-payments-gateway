"""
Pytest configuration and fixtures.
"""
from __future__ import annotations

import os
from typing import Any, AsyncGenerator

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

DEFAULT_TEST_DATABASE_URL = (
    "postgresql+asyncpg://payments_test_user:payments_test_pw@127.0.0.1:5432/payments_test"
)

TEST_DATABASE_URL = os.getenv("TEST_DATABASE_URL", DEFAULT_TEST_DATABASE_URL)

# Keep the app deterministic under pytest before importing it.
os.environ["DATABASE_URL"] = TEST_DATABASE_URL

from api.main import app  # noqa: E402
from database.connection import close_db, get_db  # noqa: E402
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
        connect_args={"timeout": 5},
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
async def client(test_db: AsyncSession) -> AsyncGenerator[AsyncClient, Any]:
    """
    Create test HTTP client with database dependency override.

    This keeps integration tests off the app's global cached engine/session
    and ties request handling to the same loop-scoped test session.
    """
    await close_db()

    async def override_get_db() -> AsyncGenerator[AsyncSession, Any]:
        yield test_db

    app.dependency_overrides[get_db] = override_get_db

    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac

    app.dependency_overrides.clear()
    await close_db()


@pytest.fixture
def sample_payment_data() -> dict[str, Any]:
    """Sample payment request data."""
    return {
        "user_id": "123e4567-e89b-12d3-a456-426614174000",
        "amount_cents": 1000,
        "currency": "USD",
        "metadata": {"order_id": "test_order_123"},
    }