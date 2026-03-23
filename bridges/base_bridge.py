"""
OpenClaw 2.0 ACI Framework - Abstract Base Bridge.

Defines the :class:`IOpenClawBridge` interface that every execution bridge
(web, desktop, CLI) must implement.  The daemon's protocol router dispatches
actions through this interface so that the upstream code is environment-agnostic.
"""

from __future__ import annotations

import abc

from core.models.schemas import ActionRequest, ActionResult, ContextPerception


class IOpenClawBridge(abc.ABC):
    """Abstract base class for all OpenClaw execution bridges.

    Subclasses are responsible for:

    * **Perception** -- capturing the current environment state (DOM tree,
      UIA control tree, CLI output, etc.) and returning a
      :class:`ContextPerception`.
    * **Action execution** -- translating an :class:`ActionRequest` into
      concrete I/O (mouse clicks, keyboard input, subprocess calls) and
      returning an :class:`ActionResult`.
    * **Rollback** -- best-effort undo of the most recent action.
    * **Connectivity** -- managing the WebSocket link to the daemon.
    """

    @abc.abstractmethod
    async def perceive(self) -> ContextPerception:
        """Capture current environment state.

        Returns:
            A :class:`ContextPerception` snapshot describing the live
            accessibility tree, window title, URL, and any other context
            the LLM needs to decide what to do next.
        """
        ...

    @abc.abstractmethod
    async def act(self, action: ActionRequest) -> ActionResult:
        """Execute an action in the environment.

        Args:
            action: The validated action request from the LLM.

        Returns:
            An :class:`ActionResult` indicating success or failure.
        """
        ...

    @abc.abstractmethod
    async def rollback(self) -> bool:
        """Attempt to undo the last action.

        Returns:
            ``True`` if the rollback was successful (or there was nothing
            to undo), ``False`` if the undo could not be performed.
        """
        ...

    @abc.abstractmethod
    async def connect(self, daemon_url: str) -> None:
        """Connect to the daemon's WebSocket endpoint.

        Args:
            daemon_url: Full WebSocket URL, e.g.
                ``ws://localhost:11434/ws/bridge/web``.
        """
        ...

    @abc.abstractmethod
    async def disconnect(self) -> None:
        """Disconnect from the daemon and release resources."""
        ...

    @abc.abstractmethod
    def is_connected(self) -> bool:
        """Check if the bridge is currently connected to the daemon.

        Returns:
            ``True`` when the WebSocket connection is open and healthy.
        """
        ...
