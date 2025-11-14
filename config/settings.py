"""Application settings using Pydantic for environment-based configuration."""
from functools import lru_cache
from typing import List

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """Application settings loaded from environment variables."""

    # Stripe Configuration
    stripe_secret_key: str = Field(..., description="Stripe secret API key (sk_test_...)")
    stripe_publishable_key: str = Field(..., description="Stripe publishable key (pk_test_...)")
    stripe_webhook_secret: str = Field(..., description="Stripe webhook signing secret")
    stripe_api_version: str = Field(default="2023-10-16", description="Stripe API version")

    # Database Configuration
    database_url: str = Field(..., description="PostgreSQL connection URL")
    database_pool_size: int = Field(default=20, description="Database connection pool size")
    database_max_overflow: int = Field(default=50, description="Max database connection overflow")
    database_echo: bool = Field(default=False, description="Echo SQL queries (debug)")

    # Redis Configuration
    redis_url: str = Field(..., description="Redis connection URL")
    redis_lock_timeout: int = Field(default=30, description="Distributed lock timeout (seconds)")

    # Message Queue Configuration
    rabbitmq_url: str = Field(..., description="RabbitMQ connection URL")

    # Application Configuration
    app_name: str = Field(default="payment-systems", description="Application name")
    app_env: str = Field(default="development", description="Environment (development/production)")
    log_level: str = Field(default="INFO", description="Logging level")
    debug: bool = Field(default=False, description="Debug mode")

    # API Configuration
    api_host: str = Field(default="0.0.0.0", description="API host")
    api_port: int = Field(default=8000, description="API port")
    api_workers: int = Field(default=4, description="Number of API workers")
    allowed_origins: str = Field(
        default="http://localhost:3000,http://localhost:8000",
        description="CORS allowed origins (comma-separated)"
    )

    # Rate Limiting
    rate_limit_per_minute: int = Field(default=100, description="API rate limit per minute")

    # Payment Processing
    payment_retry_max_attempts: int = Field(default=5, description="Max payment retry attempts")
    payment_retry_base_delay: float = Field(
        default=1.0, description="Base delay for retry backoff (seconds)"
    )
    idempotency_cache_ttl: int = Field(
        default=86400, description="Idempotency cache TTL (seconds)"
    )

    # Reconciliation
    reconciliation_schedule: str = Field(
        default="0 2 * * *", description="Reconciliation cron schedule"
    )

    # Security
    api_key_header: str = Field(default="X-API-Key", description="API key header name")

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore"
    )

    @field_validator("stripe_secret_key")
    @classmethod
    def validate_stripe_key(cls, v: str) -> str:
        """Validate that Stripe secret key starts with sk_test_ for test mode."""
        if not v.startswith("sk_test_") and not v.startswith("sk_live_"):
            raise ValueError(
                "Invalid Stripe secret key format. Must start with 'sk_test_' or 'sk_live_'"
            )
        return v

    @field_validator("log_level")
    @classmethod
    def validate_log_level(cls, v: str) -> str:
        """Validate log level."""
        valid_levels = ["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"]
        if v.upper() not in valid_levels:
            raise ValueError(f"Invalid log level. Must be one of: {valid_levels}")
        return v.upper()

    def get_allowed_origins_list(self) -> List[str]:
        """Parse allowed origins from comma-separated string."""
        return [origin.strip() for origin in self.allowed_origins.split(",")]

    @property
    def is_production(self) -> bool:
        """Check if running in production environment."""
        return self.app_env.lower() == "production"

    @property
    def is_test_mode(self) -> bool:
        """Check if using Stripe test mode."""
        return self.stripe_secret_key.startswith("sk_test_")


@lru_cache()
def get_settings() -> Settings:
    """
    Get cached settings instance.

    Uses lru_cache to ensure settings are only loaded once.
    """
    return Settings()
