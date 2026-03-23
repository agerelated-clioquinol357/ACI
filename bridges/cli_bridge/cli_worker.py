"""
OpenClaw 2.0 ACI Framework - CLI Bridge Worker.

Python-based CLI bridge that connects to the OpenClaw daemon as a ``cli``
bridge type.  Wraps subprocess calls to CLI tools (claude, gemini, shell
commands, etc.) and translates them into the OpenClaw action/perception
protocol.

Replaces the legacy PowerShell ``claude_adapter.ps1``.

Perception:
    Returns the working directory listing and recent command output.

Actions:
    Executes commands via ``subprocess`` with configurable timeout.
    Captures stdout/stderr and returns them as :class:`ActionResult`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import platform
import signal
import subprocess
import time
from pathlib import Path
from typing import Optional

try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    import websockets  # type: ignore[no-redef]
    ws_connect = websockets.connect  # type: ignore[attr-defined]
    ConnectionClosed = websockets.exceptions.ConnectionClosed  # type: ignore[attr-defined]

from core.models.schemas import (
    ActionRequest,
    ActionResult,
    ContextPerception,
    TaskState,
    UIDNode,
)

logger = logging.getLogger(__name__)

_DEFAULT_DAEMON_URL = "ws://127.0.0.1:11434/ws/bridge/cli"
_RECONNECT_DELAY_S = 3.0
_MAX_RECONNECT_ATTEMPTS = 20

# Default timeout for subprocess commands (seconds).
_DEFAULT_CMD_TIMEOUT = 60.0

# Maximum output capture length (characters).
_MAX_OUTPUT_LEN = 50_000

# Maximum number of recent commands to keep in history.
_MAX_HISTORY = 20


class CLIBridgeWorker:
    """WebSocket client that bridges the OpenClaw daemon to CLI subprocess execution.

    Lifecycle::

        worker = CLIBridgeWorker(session_id="cli-1", working_dir="/home/user/project")
        await worker.start()
        # ... runs until shutdown ...
        await worker.stop()
    """

    def __init__(
        self,
        session_id: str,
        working_dir: Optional[str] = None,
        cmd_timeout: float = _DEFAULT_CMD_TIMEOUT,
        shell: Optional[str] = None,
    ) -> None:
        self._session_id = session_id
        self._working_dir = working_dir or os.getcwd()
        self._cmd_timeout = cmd_timeout
        self._shell = shell or self._detect_shell()

        # WebSocket state.
        self._ws = None
        self._connected: bool = False
        self._running: bool = False

        # Command history: list of (command, stdout, stderr, return_code).
        self._history: list[dict] = []

        # Last action for rollback.
        self._last_action: Optional[ActionRequest] = None
        self._last_output: Optional[str] = None

    # ------------------------------------------------------------------
    # Shell detection
    # ------------------------------------------------------------------

    @staticmethod
    def _detect_shell() -> str:
        """Detect the appropriate shell for the current platform."""
        if platform.system() == "Windows":
            # Prefer PowerShell if available, fall back to cmd.
            pwsh_paths = [
                r"C:\Program Files\PowerShell\7\pwsh.exe",
                r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe",
            ]
            for p in pwsh_paths:
                if os.path.isfile(p):
                    return p
            return "cmd.exe"
        else:
            # Unix-like: prefer the user's shell, then bash, then sh.
            user_shell = os.environ.get("SHELL", "")
            if user_shell and os.path.isfile(user_shell):
                return user_shell
            for sh in ("/bin/bash", "/usr/bin/bash", "/bin/sh"):
                if os.path.isfile(sh):
                    return sh
            return "sh"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, daemon_url: str = _DEFAULT_DAEMON_URL) -> None:
        """Connect to the daemon and enter the main loop."""
        self._running = True
        logger.info(
            "CLIBridgeWorker: starting (session=%s, cwd=%s, shell=%s)",
            self._session_id, self._working_dir, self._shell,
        )

        try:
            await self._connect_loop(daemon_url)
        except asyncio.CancelledError:
            logger.info("CLIBridgeWorker: cancelled.")
        except Exception as exc:
            logger.error("CLIBridgeWorker: fatal error: %s", exc, exc_info=True)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Shut down the worker."""
        self._running = False
        self._connected = False

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("CLIBridgeWorker: stopped.")

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self, daemon_url: str) -> None:
        """Connect with automatic reconnection."""
        attempt = 0

        while self._running and attempt < _MAX_RECONNECT_ATTEMPTS:
            try:
                logger.info(
                    "CLIBridgeWorker: connecting to %s (attempt %d)...",
                    daemon_url, attempt + 1,
                )
                async with ws_connect(daemon_url) as ws:
                    self._ws = ws
                    self._connected = True
                    attempt = 0

                    await self._send_json({
                        "type": "register",
                        "bridge_type": "cli",
                        "session_id": self._session_id,
                        "metadata": {
                            "working_dir": self._working_dir,
                            "shell": self._shell,
                            "platform": platform.system(),
                        },
                    })

                    await self._message_loop()

            except ConnectionClosed as exc:
                self._connected = False
                logger.warning(
                    "CLIBridgeWorker: WebSocket closed (code=%s).",
                    getattr(exc, "code", "?"),
                )
            except OSError as exc:
                self._connected = False
                logger.warning("CLIBridgeWorker: connection error: %s", exc)
            except Exception as exc:
                self._connected = False
                logger.error("CLIBridgeWorker: unexpected error: %s", exc, exc_info=True)

            if self._running:
                attempt += 1
                await asyncio.sleep(_RECONNECT_DELAY_S)

        if attempt >= _MAX_RECONNECT_ATTEMPTS:
            logger.error("CLIBridgeWorker: max reconnection attempts exceeded.")

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _message_loop(self) -> None:
        """Listen for daemon commands."""
        async for raw_message in self._ws:
            if not self._running:
                break

            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                continue

            msg_type = msg.get("type", "")

            if msg_type == "action":
                await self._handle_action(msg)
            elif msg_type == "perceive":
                await self._handle_perceive(msg)
            elif msg_type == "rollback":
                await self._handle_rollback(msg)
            elif msg_type == "ping":
                await self._send_json({"type": "pong", "session_id": self._session_id})
            elif msg_type == "shutdown":
                logger.info("CLIBridgeWorker: received shutdown.")
                self._running = False
                break
            else:
                await self._send_json({
                    "type": "error",
                    "session_id": self._session_id,
                    "error": f"Unknown message type: {msg_type}",
                })

    # ------------------------------------------------------------------
    # Perception
    # ------------------------------------------------------------------

    async def _handle_perceive(self, msg: dict) -> None:
        """Return working directory listing and recent command output."""
        perception = await asyncio.get_event_loop().run_in_executor(
            None, self._perceive_sync
        )
        await self._send_json({
            "type": "perception",
            "session_id": self._session_id,
            "payload": perception.model_dump(),
        })

    def _perceive_sync(self) -> ContextPerception:
        """Synchronous perception (runs in thread pool)."""
        elements: list[UIDNode] = []

        try:
            # List working directory contents as UIDNode elements.
            cwd = Path(self._working_dir)
            if cwd.is_dir():
                uid_counter = 0
                for entry in sorted(cwd.iterdir()):
                    try:
                        entry_type = "dir" if entry.is_dir() else "file"
                        node = UIDNode(
                            uid=f"oc_{uid_counter}",
                            tag=entry_type,
                            role="listitem",
                            text=entry.name,
                            attributes={
                                "path": str(entry),
                                "type": entry_type,
                            },
                            interactable=True,
                        )
                        elements.append(node)
                        uid_counter += 1
                    except PermissionError:
                        continue

            # Build a summary of recent command history for context.
            history_text = ""
            if self._history:
                recent = self._history[-5:]  # Last 5 commands.
                lines = []
                for h in recent:
                    lines.append(f"$ {h['command']}")
                    if h.get("stdout"):
                        lines.append(h["stdout"][:500])
                    if h.get("stderr"):
                        lines.append(f"[stderr] {h['stderr'][:200]}")
                    lines.append(f"[exit {h.get('returncode', '?')}]")
                    lines.append("")
                history_text = "\n".join(lines)

            return ContextPerception(
                state=TaskState.IDLE,
                session_id=self._session_id,
                active_window_title=f"CLI: {self._working_dir}",
                context_env="cli",
                elements=elements,
                interrupted_reason=history_text if history_text else None,
            )

        except Exception as exc:
            logger.error("CLIBridgeWorker: perception failed: %s", exc, exc_info=True)
            return ContextPerception(
                state=TaskState.FAILED,
                session_id=self._session_id,
                context_env="cli",
                interrupted_reason=f"Perception failed: {exc}",
            )

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _handle_action(self, msg: dict) -> None:
        """Execute a CLI command action."""
        try:
            action = ActionRequest(**msg.get("payload", msg))
        except Exception as exc:
            await self._send_json({
                "type": "action_result",
                "session_id": self._session_id,
                "payload": {
                    "success": False,
                    "action_type": msg.get("payload", {}).get("action_type", "unknown"),
                    "error": f"Invalid action request: {exc}",
                },
            })
            return

        result = await asyncio.get_event_loop().run_in_executor(
            None, self._act_sync, action
        )
        self._last_action = action
        self._last_output = result.message

        await self._send_json({
            "type": "action_result",
            "session_id": self._session_id,
            "payload": result.model_dump(),
        })

    def _act_sync(self, action: ActionRequest) -> ActionResult:
        """Execute a command synchronously (runs in thread pool)."""
        start = time.perf_counter()

        # The primary action for CLI bridge is "type" which executes a command.
        # We also support "press_key" (sends to stdin) and "wait".
        if action.action_type == "wait":
            try:
                duration = float(action.value) if action.value else 1.0
            except ValueError:
                duration = 1.0
            duration = max(0.1, min(duration, 30.0))
            time.sleep(duration)
            elapsed = (time.perf_counter() - start) * 1000
            return ActionResult(
                success=True,
                action_type="wait",
                message=f"Waited {duration:.1f}s.",
                elapsed_ms=elapsed,
            )

        # For type/click/press_key actions, execute the command string.
        command = action.value or ""
        if not command:
            elapsed = (time.perf_counter() - start) * 1000
            return ActionResult(
                success=False,
                action_type=action.action_type,
                error="No command provided (value is empty).",
                elapsed_ms=elapsed,
            )

        # Handle special cd command to change working directory.
        if command.strip().startswith("cd "):
            return self._handle_cd(command.strip(), start)

        try:
            # Determine shell execution strategy.
            if platform.system() == "Windows":
                shell_cmd = [self._shell, "/c", command] if "cmd" in self._shell.lower() else [self._shell, "-Command", command]
            else:
                shell_cmd = [self._shell, "-c", command]

            proc = subprocess.run(
                shell_cmd,
                cwd=self._working_dir,
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                timeout=self._cmd_timeout,
                env={**os.environ, "OPENCLAW_SESSION": self._session_id},
            )

            stdout = (proc.stdout or "")[:_MAX_OUTPUT_LEN]
            stderr = (proc.stderr or "")[:_MAX_OUTPUT_LEN]
            success = proc.returncode == 0

            # Record in history.
            self._history.append({
                "command": command,
                "stdout": stdout,
                "stderr": stderr,
                "returncode": proc.returncode,
                "timestamp": time.time(),
            })
            if len(self._history) > _MAX_HISTORY:
                self._history = self._history[-_MAX_HISTORY:]

            elapsed = (time.perf_counter() - start) * 1000

            # Build message with both stdout and stderr.
            message_parts: list[str] = []
            if stdout:
                message_parts.append(stdout)
            if stderr:
                message_parts.append(f"[stderr]\n{stderr}")
            message_parts.append(f"[exit code: {proc.returncode}]")
            message = "\n".join(message_parts)

            return ActionResult(
                success=success,
                action_type=action.action_type,
                target_uid=action.target_uid,
                message=message[:_MAX_OUTPUT_LEN],
                error=stderr[:2000] if not success and stderr else None,
                elapsed_ms=elapsed,
            )

        except subprocess.TimeoutExpired:
            elapsed = (time.perf_counter() - start) * 1000
            self._history.append({
                "command": command,
                "stdout": "",
                "stderr": f"TIMEOUT after {self._cmd_timeout}s",
                "returncode": -1,
                "timestamp": time.time(),
            })
            return ActionResult(
                success=False,
                action_type=action.action_type,
                error=f"Command timed out after {self._cmd_timeout}s: {command[:200]}",
                elapsed_ms=elapsed,
            )

        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error("CLIBridgeWorker: command error: %s", exc, exc_info=True)
            return ActionResult(
                success=False,
                action_type=action.action_type,
                error=f"Execution error: {exc}",
                elapsed_ms=elapsed,
            )

    def _handle_cd(self, command: str, start: float) -> ActionResult:
        """Handle the cd command by changing the working directory."""
        parts = command.split(maxsplit=1)
        target = parts[1].strip().strip('"').strip("'") if len(parts) > 1 else ""

        if not target:
            target = str(Path.home())

        # Resolve relative paths against current working dir.
        target_path = Path(self._working_dir) / target
        try:
            resolved = target_path.resolve(strict=True)
            if not resolved.is_dir():
                elapsed = (time.perf_counter() - start) * 1000
                return ActionResult(
                    success=False,
                    action_type="type",
                    error=f"Not a directory: {resolved}",
                    elapsed_ms=elapsed,
                )
            self._working_dir = str(resolved)
            elapsed = (time.perf_counter() - start) * 1000
            self._history.append({
                "command": command,
                "stdout": str(resolved),
                "stderr": "",
                "returncode": 0,
                "timestamp": time.time(),
            })
            return ActionResult(
                success=True,
                action_type="type",
                message=f"Changed directory to: {resolved}",
                elapsed_ms=elapsed,
            )
        except (FileNotFoundError, OSError) as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return ActionResult(
                success=False,
                action_type="type",
                error=f"cd failed: {exc}",
                elapsed_ms=elapsed,
            )

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def _handle_rollback(self, msg: dict) -> None:
        """Rollback is limited for CLI -- we can't undo commands."""
        await self._send_json({
            "type": "rollback_result",
            "session_id": self._session_id,
            "payload": {
                "success": True,
                "message": "CLI rollback not supported. Last command output preserved in history.",
            },
        })

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    async def _send_json(self, data: dict) -> None:
        if self._ws:
            try:
                await self._ws.send(json.dumps(data, default=str, ensure_ascii=False))
            except Exception as exc:
                logger.warning("CLIBridgeWorker: send failed: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run the CLI bridge worker as a standalone process."""
    import argparse

    # Force UTF-8 on all streams to avoid GBK encoding errors on Chinese Windows.
    import io as _io, sys as _sys
    if hasattr(_sys.stderr, "buffer"):
        _sys.stderr = _io.TextIOWrapper(_sys.stderr.buffer, encoding="utf-8", errors="replace")
    if hasattr(_sys.stdout, "buffer"):
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="OpenClaw CLI Bridge Worker")
    parser.add_argument("--session-id", default="cli-default", help="Session identifier")
    parser.add_argument("--daemon-url", default=_DEFAULT_DAEMON_URL, help="Daemon WebSocket URL")
    parser.add_argument("--working-dir", default=None, help="Initial working directory")
    parser.add_argument("--timeout", type=float, default=_DEFAULT_CMD_TIMEOUT, help="Command timeout (seconds)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    worker = CLIBridgeWorker(
        session_id=args.session_id,
        working_dir=args.working_dir,
        cmd_timeout=args.timeout,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))
        except NotImplementedError:
            pass

    await worker.start(daemon_url=args.daemon_url)


if __name__ == "__main__":
    asyncio.run(main())
