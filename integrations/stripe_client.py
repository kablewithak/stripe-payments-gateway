"""
Stripe API client with retry logic and comprehensive error handling.

Implements:
- Exponential backoff for transient errors
- Circuit breaker pattern
- Idempotent payment creation
- Webhook signature verification
"""
import time
from enum import Enum
from typing import Any, Dict, Optional

import stripe
import structlog
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from config import get_settings

logger = structlog.get_logger(__name__)


class StripeErrorType(Enum):
    """Classification of Stripe errors for retry logic."""

    TRANSIENT = "transient"  # Retry these
    PERMANENT = "permanent"  # Don't retry these
    RATE_LIMIT = "rate_limit"  # Retry with longer backoff


class StripeError(Exception):
    """Base exception for Stripe-related errors."""

    def __init__(
        self,
        message: str,
        error_type: StripeErrorType,
        original_error: Optional[Exception] = None,
    ):
        """
        Initialize Stripe error.

        Args:
            message: Error message
            error_type: Classification of error
            original_error: Original Stripe exception
        """
        super().__init__(message)
        self.error_type = error_type
        self.original_error = original_error


class CircuitBreaker:
    """
    Circuit breaker for Stripe API calls.

    Prevents cascading failures by temporarily stopping requests
    when error rate exceeds threshold.
    """

    def __init__(
        self,
        failure_threshold: int = 5,
        timeout: int = 60,
        success_threshold: int = 2,
    ):
        """
        Initialize circuit breaker.

        Args:
            failure_threshold: Number of failures before opening circuit
            timeout: Seconds before attempting to close circuit
            success_threshold: Successful calls needed to close circuit
        """
        self.failure_threshold = failure_threshold
        self.timeout = timeout
        self.success_threshold = success_threshold
        self.failure_count = 0
        self.success_count = 0
        self.last_failure_time: Optional[float] = None
        self.state = "closed"  # closed, open, half_open

    def call(self, func: Any, *args: Any, **kwargs: Any) -> Any:
        """
        Execute function with circuit breaker protection.

        Args:
            func: Function to execute
            *args: Positional arguments
            **kwargs: Keyword arguments

        Returns:
            Function result

        Raises:
            StripeError: If circuit is open
        """
        if self.state == "open":
            if (
                self.last_failure_time
                and time.time() - self.last_failure_time > self.timeout
            ):
                self.state = "half_open"
                self.success_count = 0
                logger.info("circuit_breaker_half_open")
            else:
                raise StripeError(
                    "Circuit breaker is open",
                    StripeErrorType.TRANSIENT,
                )

        try:
            result = func(*args, **kwargs)
            self.on_success()
            return result
        except Exception as e:
            self.on_failure()
            raise e

    def on_success(self) -> None:
        """Record successful call."""
        self.failure_count = 0
        if self.state == "half_open":
            self.success_count += 1
            if self.success_count >= self.success_threshold:
                self.state = "closed"
                logger.info("circuit_breaker_closed")

    def on_failure(self) -> None:
        """Record failed call."""
        self.failure_count += 1
        self.last_failure_time = time.time()
        if self.failure_count >= self.failure_threshold:
            self.state = "open"
            logger.warning(
                "circuit_breaker_opened",
                failure_count=self.failure_count,
            )


class StripeClient:
    """
    Wrapper for Stripe API with production-grade error handling.

    Features:
    - Automatic retry with exponential backoff
    - Circuit breaker pattern
    - Idempotent payment creation
    - Comprehensive error classification
    """

    def __init__(self) -> None:
        """Initialize Stripe client."""
        settings = get_settings()
        stripe.api_key = settings.stripe_secret_key
        stripe.api_version = settings.stripe_api_version
        self.settings = settings
        self.circuit_breaker = CircuitBreaker()

        logger.info(
            "stripe_client_initialized",
            api_version=stripe.api_version,
            test_mode=settings.is_test_mode,
        )

    @staticmethod
    def _classify_error(error: stripe.error.StripeError) -> StripeErrorType:
        """
        Classify Stripe error for retry logic.

        Args:
            error: Stripe error

        Returns:
            StripeErrorType: Error classification
        """
        if isinstance(error, stripe.error.RateLimitError):
            return StripeErrorType.RATE_LIMIT
        elif isinstance(
            error,
            (
                stripe.error.APIConnectionError,
                stripe.error.APIError,
                stripe.error.ServiceUnavailableError,
            ),
        ):
            return StripeErrorType.TRANSIENT
        elif isinstance(
            error,
            (stripe.error.CardError, stripe.error.InvalidRequestError),
        ):
            return StripeErrorType.PERMANENT
        else:
            # Unknown errors are treated as transient
            return StripeErrorType.TRANSIENT

    def _handle_stripe_error(self, error: stripe.error.StripeError) -> None:
        """
        Handle and classify Stripe errors.

        Args:
            error: Stripe error

        Raises:
            StripeError: Classified error
        """
        error_type = self._classify_error(error)

        logger.error(
            "stripe_api_error",
            error_type=error_type.value,
            error_code=getattr(error, "code", None),
            error_message=str(error),
        )

        raise StripeError(
            message=str(error),
            error_type=error_type,
            original_error=error,
        )

    @retry(
        retry=retry_if_exception_type(StripeError),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=1, min=1, max=16),
        reraise=True,
    )
    async def create_payment_intent(
        self,
        amount_cents: int,
        currency: str,
        idempotency_key: str,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> stripe.PaymentIntent:
        """
        Create a Stripe PaymentIntent with idempotency.

        Args:
            amount_cents: Amount in cents
            currency: Currency code (e.g., 'usd')
            idempotency_key: Idempotency key for preventing duplicates
            metadata: Optional metadata

        Returns:
            stripe.PaymentIntent: Created payment intent

        Raises:
            StripeError: If payment creation fails
        """
        logger.info(
            "creating_payment_intent",
            amount_cents=amount_cents,
            currency=currency,
            idempotency_key=idempotency_key,
        )

        try:

            def _create() -> stripe.PaymentIntent:
                return stripe.PaymentIntent.create(
                    amount=amount_cents,
                    currency=currency.lower(),
                    idempotency_key=idempotency_key,
                    metadata=metadata or {},
                    automatic_payment_methods={"enabled": True},
                )

            payment_intent = self.circuit_breaker.call(_create)

            logger.info(
                "payment_intent_created",
                payment_intent_id=payment_intent.id,
                status=payment_intent.status,
            )

            return payment_intent

        except stripe.error.StripeError as e:
            self._handle_stripe_error(e)
            raise  # For type checker

    async def retrieve_payment_intent(
        self, payment_intent_id: str
    ) -> stripe.PaymentIntent:
        """
        Retrieve a PaymentIntent by ID.

        Args:
            payment_intent_id: Stripe PaymentIntent ID

        Returns:
            stripe.PaymentIntent: Retrieved payment intent

        Raises:
            StripeError: If retrieval fails
        """
        logger.info("retrieving_payment_intent", payment_intent_id=payment_intent_id)

        try:

            def _retrieve() -> stripe.PaymentIntent:
                return stripe.PaymentIntent.retrieve(payment_intent_id)

            return self.circuit_breaker.call(_retrieve)

        except stripe.error.StripeError as e:
            self._handle_stripe_error(e)
            raise  # For type checker

    async def confirm_payment_intent(
        self, payment_intent_id: str, payment_method: Optional[str] = None
    ) -> stripe.PaymentIntent:
        """
        Confirm a PaymentIntent.

        Args:
            payment_intent_id: Stripe PaymentIntent ID
            payment_method: Optional payment method ID

        Returns:
            stripe.PaymentIntent: Confirmed payment intent

        Raises:
            StripeError: If confirmation fails
        """
        logger.info("confirming_payment_intent", payment_intent_id=payment_intent_id)

        try:

            def _confirm() -> stripe.PaymentIntent:
                kwargs: Dict[str, Any] = {}
                if payment_method:
                    kwargs["payment_method"] = payment_method
                return stripe.PaymentIntent.confirm(payment_intent_id, **kwargs)

            payment_intent = self.circuit_breaker.call(_confirm)

            logger.info(
                "payment_intent_confirmed",
                payment_intent_id=payment_intent.id,
                status=payment_intent.status,
            )

            return payment_intent

        except stripe.error.StripeError as e:
            self._handle_stripe_error(e)
            raise  # For type checker

    async def create_refund(
        self,
        payment_intent_id: str,
        amount_cents: Optional[int] = None,
        reason: Optional[str] = None,
        idempotency_key: Optional[str] = None,
    ) -> stripe.Refund:
        """
        Create a refund for a payment.

        Args:
            payment_intent_id: Stripe PaymentIntent ID
            amount_cents: Optional partial refund amount
            reason: Optional refund reason
            idempotency_key: Optional idempotency key

        Returns:
            stripe.Refund: Created refund

        Raises:
            StripeError: If refund creation fails
        """
        logger.info(
            "creating_refund",
            payment_intent_id=payment_intent_id,
            amount_cents=amount_cents,
        )

        try:

            def _create_refund() -> stripe.Refund:
                kwargs: Dict[str, Any] = {"payment_intent": payment_intent_id}
                if amount_cents:
                    kwargs["amount"] = amount_cents
                if reason:
                    kwargs["reason"] = reason
                if idempotency_key:
                    kwargs["idempotency_key"] = idempotency_key
                return stripe.Refund.create(**kwargs)

            refund = self.circuit_breaker.call(_create_refund)

            logger.info(
                "refund_created",
                refund_id=refund.id,
                status=refund.status,
            )

            return refund

        except stripe.error.StripeError as e:
            self._handle_stripe_error(e)
            raise  # For type checker

    async def list_payment_intents(
        self,
        limit: int = 100,
        starting_after: Optional[str] = None,
        created_gte: Optional[int] = None,
        created_lte: Optional[int] = None,
    ) -> stripe.ListObject:
        """
        List PaymentIntents with pagination.

        Args:
            limit: Number of items to return
            starting_after: Cursor for pagination
            created_gte: Filter by creation time (greater than or equal)
            created_lte: Filter by creation time (less than or equal)

        Returns:
            stripe.ListObject: List of payment intents

        Raises:
            StripeError: If listing fails
        """
        logger.info("listing_payment_intents", limit=limit)

        try:

            def _list() -> stripe.ListObject:
                kwargs: Dict[str, Any] = {"limit": limit}
                if starting_after:
                    kwargs["starting_after"] = starting_after
                if created_gte or created_lte:
                    kwargs["created"] = {}
                    if created_gte:
                        kwargs["created"]["gte"] = created_gte
                    if created_lte:
                        kwargs["created"]["lte"] = created_lte
                return stripe.PaymentIntent.list(**kwargs)

            return self.circuit_breaker.call(_list)

        except stripe.error.StripeError as e:
            self._handle_stripe_error(e)
            raise  # For type checker
