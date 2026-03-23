"""
OpenClaw 2.0 ACI Framework — Interrupt-Protection State Machine.

Provides per-session task-state tracking with a frozen-context call stack
so that UI interrupts can be pushed/popped without losing in-flight work.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from .models.schemas import TaskState, UIInterruptEvent

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Legal state transitions
# ---------------------------------------------------------------------------
VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.IDLE:            {TaskState.EXECUTING},
    TaskState.EXECUTING:       {TaskState.IDLE, TaskState.BLOCKED_BY_UI,
                                TaskState.AWAIT_SUBTASK, TaskState.FAILED,
                                TaskState.COMPLETED},
    TaskState.BLOCKED_BY_UI:   {TaskState.EXECUTING, TaskState.FAILED},
    TaskState.AWAIT_SUBTASK:   {TaskState.EXECUTING, TaskState.FAILED},
    TaskState.FAILED:          {TaskState.IDLE},
    TaskState.COMPLETED:       {TaskState.IDLE},
}


class ACIStateMachine:
    """Per-session finite-state machine with interrupt call-stack support.

    Every session tracked by this machine has:
    * A current :class:`TaskState`.
    * A LIFO call stack of frozen context dicts that are pushed when UI
      interrupts arrive and popped when those interrupts are resolved.

    All public methods are coroutines guarded by a single
    :class:`asyncio.Lock` to prevent concurrent state corruption.
    """

    def __init__(self) -> None:
        self._sessions: dict[str, TaskState] = {}
        self._call_stacks: dict[str, list[dict]] = {}
        self._lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create_session(self, session_id: str) -> None:
        """Register a new session in the ``IDLE`` state.

        Raises:
            ValueError: If *session_id* is already registered.
        """
        async with self._lock:
            if session_id in self._sessions:
                raise ValueError(
                    f"Session '{session_id}' already exists in the state machine"
                )
            self._sessions[session_id] = TaskState.IDLE
            self._call_stacks[session_id] = []
            logger.info("State machine: session '%s' created (state=IDLE)", session_id)

    async def remove_session(self, session_id: str) -> None:
        """Remove *session_id* and its call stack.

        This is idempotent — removing a non-existent session is a no-op.
        """
        async with self._lock:
            removed_state = self._sessions.pop(session_id, None)
            removed_stack = self._call_stacks.pop(session_id, None)
            if removed_state is not None:
                depth = len(removed_stack) if removed_stack else 0
                logger.info(
                    "State machine: session '%s' removed "
                    "(final_state=%s, stack_depth=%d)",
                    session_id, removed_state.value, depth,
                )
            else:
                logger.debug(
                    "State machine: remove_session('%s') called but session "
                    "did not exist",
                    session_id,
                )

    # ------------------------------------------------------------------
    # State queries & transitions
    # ------------------------------------------------------------------

    async def get_state(self, session_id: str) -> TaskState:
        """Return the current :class:`TaskState` for *session_id*.

        Raises:
            KeyError: If *session_id* is not registered.
        """
        async with self._lock:
            try:
                return self._sessions[session_id]
            except KeyError:
                raise KeyError(
                    f"Unknown session: '{session_id}'"
                ) from None

    async def transition(self, session_id: str, new_state: TaskState) -> None:
        """Attempt to move *session_id* to *new_state*.

        Raises:
            KeyError: If *session_id* is not registered.
            ValueError: If the transition from the current state to
                *new_state* is not in :data:`VALID_TRANSITIONS`.
        """
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise KeyError(f"Unknown session: '{session_id}'")

            allowed = VALID_TRANSITIONS.get(current, set())
            if new_state not in allowed:
                allowed_str = (
                    ", ".join(sorted(s.value for s in allowed))
                    if allowed
                    else "<none>"
                )
                raise ValueError(
                    f"Illegal transition for session '{session_id}': "
                    f"{current.value} -> {new_state.value}  "
                    f"(allowed targets from {current.value}: {allowed_str})"
                )

            self._sessions[session_id] = new_state
            logger.debug(
                "State machine: session '%s' transitioned %s -> %s",
                session_id, current.value, new_state.value,
            )

    # ------------------------------------------------------------------
    # Interrupt stack management
    # ------------------------------------------------------------------

    async def push_interrupt(
        self,
        session_id: str,
        interrupt: UIInterruptEvent,
        frozen_context: dict,
    ) -> dict:
        """Freeze the current execution context and block on a UI interrupt.

        Steps performed atomically:

        1. Validates that transitioning to ``BLOCKED_BY_UI`` is legal.
        2. Pushes *frozen_context* onto the session's call stack.
        3. Sets the session state to ``BLOCKED_BY_UI``.
        4. Returns a descriptor dict the LLM can use to handle the interrupt.

        Raises:
            KeyError: If *session_id* is not registered.
            ValueError: If the current state does not permit blocking.
        """
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise KeyError(f"Unknown session: '{session_id}'")

            allowed = VALID_TRANSITIONS.get(current, set())
            if TaskState.BLOCKED_BY_UI not in allowed:
                raise ValueError(
                    f"Cannot push interrupt for session '{session_id}' in "
                    f"state {current.value} (BLOCKED_BY_UI is not a valid "
                    f"transition target)"
                )

            self._call_stacks[session_id].append(frozen_context)
            self._sessions[session_id] = TaskState.BLOCKED_BY_UI

            depth = len(self._call_stacks[session_id])
            logger.info(
                "State machine: session '%s' interrupted — pushed context "
                "(stack_depth=%d, interrupt_type=%s)",
                session_id, depth, interrupt.interrupt_type,
            )

            return {
                "session_id": session_id,
                "interrupt": interrupt.model_dump(),
                "stack_depth": depth,
                "previous_state": current.value,
                "message": (
                    "Task execution suspended. Awaiting UI interrupt "
                    "resolution before the original task can resume."
                ),
            }

    async def pop_resolved(self, session_id: str) -> Optional[dict]:
        """Pop the most recent frozen context and resume execution.

        The session state is moved back to ``EXECUTING`` so the original
        task can continue.

        Returns:
            The frozen context ``dict`` that was saved during
            :meth:`push_interrupt`, or ``None`` if the stack is empty.

        Raises:
            KeyError: If *session_id* is not registered.
        """
        async with self._lock:
            current = self._sessions.get(session_id)
            if current is None:
                raise KeyError(f"Unknown session: '{session_id}'")

            stack = self._call_stacks.get(session_id, [])
            if not stack:
                logger.warning(
                    "State machine: pop_resolved('%s') called but the "
                    "call stack is empty",
                    session_id,
                )
                return None

            frozen_context = stack.pop()
            self._sessions[session_id] = TaskState.EXECUTING

            logger.info(
                "State machine: session '%s' interrupt resolved — restored "
                "context (remaining_stack_depth=%d)",
                session_id, len(stack),
            )
            return frozen_context

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    async def get_stack_depth(self, session_id: str) -> int:
        """Return the number of frozen contexts on the call stack.

        Raises:
            KeyError: If *session_id* is not registered.
        """
        async with self._lock:
            stack = self._call_stacks.get(session_id)
            if stack is None:
                raise KeyError(f"Unknown session: '{session_id}'")
            return len(stack)
