"""
Pytest configuration and fixtures.
"""
import asyncio
from typing import AsyncGenerator, Any

import pytest
import pytest_asyncio
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, create_async_engine
from sqlalchemy.pool import NullPool

from api.main import app
from config import Settings
from database.connection import get_session_factory
from database.models import Base


@pytest.fixture(scope="session")
def event_loop() -> asyncio.AbstractEventLoop:
    """Create event loop for async tests."""
    loop = asyncio.get_event_loop_policy().new_event_loop()
    yield loop
    loop.close()


@pytest.fixture(scope="session")
def test_settings() -> Settings:
    """Create test settings."""
    return Settings(
        stripe_secret_key="sk_test_fake_key_for_testing",
        stripe_publishable_key="pk_test_fake_key_for_testing",
        stripe_webhook_secret="whsec_test_fake_secret",
        database_url="postgresql+asyncpg://postgres:postgres@localhost:5432/payments_test",
        redis_url="redis://localhost:6379/1",
        rabbitmq_url="amqp://guest:guest@localhost:5672/",
        app_name="payment-systems-test",
        app_env="test",
        log_level="DEBUG",
        debug=True,
    )


@pytest_asyncio.fixture
async def test_db() -> AsyncGenerator[AsyncSession, Any]:
    """Create test database session."""
    # Create test engine
    engine = create_async_engine(
        "postgresql+asyncpg://postgres:postgres@localhost:5432/payments_test",
        poolclass=NullPool,
    )

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.drop_all)
        await conn.run_sync(Base.metadata.create_all)

    # Create session factory
    from sqlalchemy.ext.asyncio import async_sessionmaker

    session_factory = async_sessionmaker(
        engine, class_=AsyncSession, expire_on_commit=False
    )

    async with session_factory() as session:
        yield session
        await session.rollback()

    # Cleanup
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
