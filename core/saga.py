"""
Saga orchestration for complex payment workflows.

Implements the Saga pattern for distributed transactions with compensating actions.
Example workflow: Payment → Inventory Reservation → Fulfillment → Confirmation
"""
import uuid
from datetime import datetime
from enum import Enum
from typing import Any, Callable, Dict, List, Optional

import structlog

logger = structlog.get_logger(__name__)


class SagaState(Enum):
    """Saga execution states."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"
    FAILED = "failed"


class StepStatus(Enum):
    """Step execution status."""

    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"


class SagaStep:
    """
    Represents a single step in a saga.

    Each step has:
    - Forward action (the main operation)
    - Compensating action (rollback/undo operation)
    """

    def __init__(
        self,
        name: str,
        forward_action: Callable,
        compensating_action: Optional[Callable] = None,
        timeout_seconds: int = 30,
    ):
        """
        Initialize saga step.

        Args:
            name: Step name
            forward_action: Async function to execute
            compensating_action: Async function to compensate/rollback
            timeout_seconds: Step timeout
        """
        self.name = name
        self.forward_action = forward_action
        self.compensating_action = compensating_action
        self.timeout_seconds = timeout_seconds
        self.status = StepStatus.PENDING
        self.result: Optional[Any] = None
        self.error: Optional[str] = None

    async def execute(self, context: Dict[str, Any]) -> Any:
        """
        Execute the forward action.

        Args:
            context: Shared saga context

        Returns:
            Any: Step result

        Raises:
            Exception: If step execution fails
        """
        logger.info("saga_step_executing", step=self.name)

        try:
            self.result = await self.forward_action(context)
            self.status = StepStatus.COMPLETED
            logger.info("saga_step_completed", step=self.name)
            return self.result
        except Exception as e:
            self.status = StepStatus.FAILED
            self.error = str(e)
            logger.error("saga_step_failed", step=self.name, error=str(e))
            raise

    async def compensate(self, context: Dict[str, Any]) -> None:
        """
        Execute the compensating action.

        Args:
            context: Shared saga context
        """
        if self.compensating_action is None:
            logger.warning("saga_step_no_compensation", step=self.name)
            return

        if self.status != StepStatus.COMPLETED:
            logger.info("saga_step_skip_compensation", step=self.name, status=self.status.value)
            return

        logger.info("saga_step_compensating", step=self.name)

        try:
            await self.compensating_action(context, self.result)
            self.status = StepStatus.COMPENSATED
            logger.info("saga_step_compensated", step=self.name)
        except Exception as e:
            logger.error("saga_step_compensation_failed", step=self.name, error=str(e))
            # Compensation failures are logged but don't fail the saga
            # (they may require manual intervention)


class Saga:
    """
    Represents a saga (distributed transaction).

    Orchestrates multiple steps with compensating actions for rollback.
    """

    def __init__(self, saga_id: Optional[str] = None, name: Optional[str] = None):
        """
        Initialize saga.

        Args:
            saga_id: Optional saga ID (generated if not provided)
            name: Optional saga name
        """
        self.saga_id = saga_id or str(uuid.uuid4())
        self.name = name or "unnamed_saga"
        self.steps: List[SagaStep] = []
        self.state = SagaState.PENDING
        self.context: Dict[str, Any] = {}
        self.created_at = datetime.utcnow()
        self.completed_at: Optional[datetime] = None

        logger.info("saga_created", saga_id=self.saga_id, name=self.name)

    def add_step(
        self,
        name: str,
        forward_action: Callable,
        compensating_action: Optional[Callable] = None,
        timeout_seconds: int = 30,
    ) -> "Saga":
        """
        Add a step to the saga.

        Args:
            name: Step name
            forward_action: Forward action function
            compensating_action: Compensating action function
            timeout_seconds: Step timeout

        Returns:
            Saga: Self for method chaining
        """
        step = SagaStep(
            name=name,
            forward_action=forward_action,
            compensating_action=compensating_action,
            timeout_seconds=timeout_seconds,
        )
        self.steps.append(step)
        logger.info("saga_step_added", saga_id=self.saga_id, step=name)
        return self

    async def execute(self) -> Dict[str, Any]:
        """
        Execute the saga.

        Executes all steps in order. If any step fails, executes
        compensating actions in reverse order.

        Returns:
            Dict[str, Any]: Saga execution result
        """
        logger.info("saga_execution_started", saga_id=self.saga_id, name=self.name)

        self.state = SagaState.IN_PROGRESS
        completed_steps = []

        try:
            # Execute steps in order
            for step in self.steps:
                result = await step.execute(self.context)
                completed_steps.append(step)

                # Store result in context for subsequent steps
                self.context[f"{step.name}_result"] = result

            # All steps completed successfully
            self.state = SagaState.COMPLETED
            self.completed_at = datetime.utcnow()

            logger.info(
                "saga_completed_successfully",
                saga_id=self.saga_id,
                steps_completed=len(completed_steps),
            )

            return {
                "saga_id": self.saga_id,
                "state": self.state.value,
                "steps_completed": len(completed_steps),
                "context": self.context,
            }

        except Exception as e:
            logger.error("saga_execution_failed", saga_id=self.saga_id, error=str(e))

            # Compensate completed steps in reverse order
            self.state = SagaState.COMPENSATING
            await self._compensate(completed_steps)

            self.state = SagaState.COMPENSATED
            self.completed_at = datetime.utcnow()

            return {
                "saga_id": self.saga_id,
                "state": self.state.value,
                "error": str(e),
                "steps_compensated": len(completed_steps),
            }

    async def _compensate(self, completed_steps: List[SagaStep]) -> None:
        """
        Execute compensating actions for completed steps.

        Args:
            completed_steps: List of completed steps to compensate
        """
        logger.info(
            "saga_compensation_started",
            saga_id=self.saga_id,
            steps_to_compensate=len(completed_steps),
        )

        # Compensate in reverse order
        for step in reversed(completed_steps):
            try:
                await step.compensate(self.context)
            except Exception as e:
                logger.error(
                    "saga_compensation_error",
                    saga_id=self.saga_id,
                    step=step.name,
                    error=str(e),
                )

        logger.info("saga_compensation_completed", saga_id=self.saga_id)


class SagaOrchestrator:
    """
    Orchestrates and tracks multiple sagas.

    Provides saga lifecycle management and monitoring.
    """

    def __init__(self) -> None:
        """Initialize saga orchestrator."""
        self.active_sagas: Dict[str, Saga] = {}
        logger.info("saga_orchestrator_initialized")

    def create_saga(self, name: str) -> Saga:
        """
        Create a new saga.

        Args:
            name: Saga name

        Returns:
            Saga: Created saga instance
        """
        saga = Saga(name=name)
        self.active_sagas[saga.saga_id] = saga
        return saga

    async def execute_saga(self, saga: Saga) -> Dict[str, Any]:
        """
        Execute a saga and track it.

        Args:
            saga: Saga to execute

        Returns:
            Dict[str, Any]: Execution result
        """
        result = await saga.execute()

        # Remove from active sagas
        if saga.saga_id in self.active_sagas:
            del self.active_sagas[saga.saga_id]

        return result

    def get_saga(self, saga_id: str) -> Optional[Saga]:
        """
        Get a saga by ID.

        Args:
            saga_id: Saga ID

        Returns:
            Optional[Saga]: Saga instance or None
        """
        return self.active_sagas.get(saga_id)

    def get_active_sagas_count(self) -> int:
        """
        Get count of active sagas.

        Returns:
            int: Number of active sagas
        """
        return len(self.active_sagas)


# Example usage:
async def example_payment_saga() -> None:
    """
    Example: Payment processing saga with inventory reservation.
    """
    orchestrator = SagaOrchestrator()
    saga = orchestrator.create_saga("payment_with_inventory")

    # Step 1: Create payment
    async def create_payment(ctx: Dict[str, Any]) -> Dict[str, Any]:
        # Payment creation logic
        return {"payment_id": "pay_123", "amount": 1000}

    async def refund_payment(ctx: Dict[str, Any], result: Any) -> None:
        # Refund logic
        logger.info("refunding_payment", payment_id=result["payment_id"])

    # Step 2: Reserve inventory
    async def reserve_inventory(ctx: Dict[str, Any]) -> Dict[str, Any]:
        # Inventory reservation logic
        return {"reservation_id": "res_456"}

    async def release_inventory(ctx: Dict[str, Any], result: Any) -> None:
        # Release inventory logic
        logger.info("releasing_inventory", reservation_id=result["reservation_id"])

    # Step 3: Confirm order
    async def confirm_order(ctx: Dict[str, Any]) -> Dict[str, Any]:
        # Order confirmation logic
        return {"order_id": "ord_789"}

    # Build saga
    saga.add_step("create_payment", create_payment, refund_payment)
    saga.add_step("reserve_inventory", reserve_inventory, release_inventory)
    saga.add_step("confirm_order", confirm_order)

    # Execute
    result = await orchestrator.execute_saga(saga)
    logger.info("saga_result", result=result)
