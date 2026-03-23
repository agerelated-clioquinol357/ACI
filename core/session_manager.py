"""
OpenClaw 2.0 ACI Framework — Session Lifecycle & I/O Mutex Manager.

Manages the full lifecycle of ACI sessions: creation, activity tracking,
per-session I/O locking (to prevent two agents clicking simultaneously),
and orderly teardown.  Composes an :class:`ACIStateMachine` for
state-transition control.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional

from .models.schemas import SessionConfig, TaskState, ContextPerception
from .state_machine import ACIStateMachine

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Session descriptor (module-level dataclass)
# ---------------------------------------------------------------------------

@dataclass
class SessionInfo:
    """Metadata and runtime objects associated with a single ACI session."""

    session_id: str
    config: SessionConfig
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    last_activity: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    io_lock: asyncio.Lock = field(default_factory=asyncio.Lock)

    def to_summary_dict(self) -> dict:
        """Return a JSON-safe summary of this session (excludes the lock)."""
        return {
            "session_id": self.session_id,
            "context_env": self.config.context_env,
            "created_at": self.created_at.isoformat(),
            "last_activity": self.last_activity.isoformat(),
            "metadata": self.config.metadata,
        }


# ---------------------------------------------------------------------------
# Session Manager
# ---------------------------------------------------------------------------

class SessionManager:
    """Global session lifecycle and I/O mutex management.

    This is the top-level orchestrator that coordinates:

    * Session creation / deletion with duplicate-ID protection.
    * Per-session I/O locks so that only one agent can drive a given
      session's UI at a time.
    * Delegation to an :class:`ACIStateMachine` for state transitions.
    * Activity tracking for idle-session reaping (external caller can
      use :pyattr:`last_activity` for that decision).
    """

    def __init__(self) -> None:
        self._sessions: dict[str, SessionInfo] = {}
        self._state_machine: ACIStateMachine = ACIStateMachine()
        self._global_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Session lifecycle
    # ------------------------------------------------------------------

    async def create_session(self, config: SessionConfig) -> str:
        """Create a new session and initialise its state-machine entry.

        Args:
            config: Session configuration (must include a unique
                ``session_id``).

        Returns:
            The ``session_id`` string.

        Raises:
            ValueError: If a session with the same ID already exists.
        """
        async with self._global_lock:
            sid = config.session_id
            if sid in self._sessions:
                raise ValueError(
                    f"Session '{sid}' already exists. Choose a different "
                    f"session_id or close the existing session first."
                )

            now = datetime.now(timezone.utc)
            info = SessionInfo(
                session_id=sid,
                config=config,
                created_at=now,
                last_activity=now,
            )
            self._sessions[sid] = info

        # State-machine initialisation is done outside the global lock
        # because `ACIStateMachine` has its own internal lock.
        await self._state_machine.create_session(sid)

        logger.info(
            "SessionManager: session '%s' created (env=%s)",
            sid, config.context_env,
        )
        return sid

    async def get_session(self, session_id: str) -> SessionInfo:
        """Retrieve the :class:`SessionInfo` for *session_id*.

        Raises:
            KeyError: If *session_id* is not registered.
        """
        try:
            return self._sessions[session_id]
        except KeyError:
            raise KeyError(
                f"Session '{session_id}' not found"
            ) from None

    # ------------------------------------------------------------------
    # I/O mutex
    # ------------------------------------------------------------------

    async def acquire_io(self, session_id: str) -> None:
        """Acquire the per-session I/O lock.

        This prevents two agents from issuing physical input to the same
        session simultaneously (e.g., two mouse clicks racing).

        Raises:
            KeyError: If *session_id* is not registered.
        """
        info = await self.get_session(session_id)
        await info.io_lock.acquire()
        logger.debug("SessionManager: I/O lock acquired for '%s'", session_id)

    async def release_io(self, session_id: str) -> None:
        """Release the per-session I/O lock.

        Raises:
            KeyError: If *session_id* is not registered.
            RuntimeError: If the lock is not currently held.
        """
        info = await self.get_session(session_id)
        try:
            info.io_lock.release()
            logger.debug("SessionManager: I/O lock released for '%s'", session_id)
        except RuntimeError:
            logger.error(
                "SessionManager: attempted to release an unheld I/O lock "
                "for session '%s'",
                session_id,
            )
            raise

    # ------------------------------------------------------------------
    # State-machine accessor
    # ------------------------------------------------------------------

    def get_state_machine(self) -> ACIStateMachine:
        """Return the composed :class:`ACIStateMachine` instance."""
        return self._state_machine

    # ------------------------------------------------------------------
    # Enumeration & activity tracking
    # ------------------------------------------------------------------

    async def list_sessions(self) -> list[dict]:
        """Return summary dicts for every active session.

        Each dict contains: ``session_id``, ``context_env``,
        ``created_at``, ``last_activity``, and ``metadata``.
        """
        summaries: list[dict] = []
        for info in self._sessions.values():
            summary = info.to_summary_dict()
            # Enrich with current state from the state machine.
            try:
                state = await self._state_machine.get_state(info.session_id)
                summary["state"] = state.value
            except KeyError:
                summary["state"] = "unknown"
            summaries.append(summary)
        return summaries

    async def touch_session(self, session_id: str) -> None:
        """Update the ``last_activity`` timestamp for *session_id*.

        Raises:
            KeyError: If *session_id* is not registered.
        """
        info = await self.get_session(session_id)
        info.last_activity = datetime.now(timezone.utc)
        logger.debug("SessionManager: touched session '%s'", session_id)

    # ------------------------------------------------------------------
    # Teardown
    # ------------------------------------------------------------------

    async def close_session(self, session_id: str) -> None:
        """Close and clean up a single session.

        Removes the session from both the local registry and the
        state machine.  If the session's I/O lock is currently held it
        will be released to avoid deadlocks on shutdown.

        Raises:
            KeyError: If *session_id* is not registered.
        """
        async with self._global_lock:
            info = self._sessions.pop(session_id, None)
            if info is None:
                raise KeyError(f"Session '{session_id}' not found")

            # Defensively release the I/O lock if it is held.
            if info.io_lock.locked():
                try:
                    info.io_lock.release()
                except RuntimeError:
                    pass  # Lock wasn't held by us — nothing to do.

        await self._state_machine.remove_session(session_id)
        logger.info("SessionManager: session '%s' closed", session_id)

    async def close_all(self) -> None:
        """Close every active session and reset internal state."""
        async with self._global_lock:
            session_ids = list(self._sessions.keys())

        for sid in session_ids:
            try:
                await self.close_session(sid)
            except KeyError:
                # Already removed (e.g., concurrent close) — safe to ignore.
                pass

        logger.info(
            "SessionManager: all sessions closed (%d total)", len(session_ids)
        )
