"""
OpenClaw 2.0 ACI Framework - Web Bridge Worker.

WebSocket client that connects to the OpenClaw daemon and drives a headless
(or headed) Chromium browser via Playwright.  Receives ``action`` and
``perceive`` commands from the daemon, delegates to :class:`WebExecutor`,
and forwards :class:`UIInterruptEvent` objects detected by
:class:`MutationShield`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import signal
import sys
import time
from typing import Optional

try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    import websockets  # type: ignore[no-redef]
    ws_connect = websockets.connect  # type: ignore[attr-defined]
    ConnectionClosed = websockets.exceptions.ConnectionClosed  # type: ignore[attr-defined]

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from core.models.schemas import ActionRequest, UIInterruptEvent

from .executor import WebExecutor
from .mutation_shield import MutationShield

logger = logging.getLogger(__name__)

# Default connection settings.
_DEFAULT_DAEMON_URL = "ws://127.0.0.1:11434/ws/bridge/web"
_RECONNECT_BASE_S = 2.0
_RECONNECT_MAX_S = 30.0
_MAX_RECONNECT_ATTEMPTS = 20
_PING_INTERVAL_S = 25.0
_MIN_INTERRUPT_INTERVAL_S = 0.5  # Max 2 interrupt forwards per second.


class WebBridgeWorker:
    """Long-running WebSocket client that bridges the daemon to a Playwright browser.

    Lifecycle::

        worker = WebBridgeWorker(session_id="demo-1")
        await worker.start(daemon_url="ws://localhost:11434/ws/bridge/web")
        # ... runs until shutdown ...
        await worker.stop()

    The worker:

    1. Launches a Playwright Chromium browser.
    2. Opens a WebSocket connection to the daemon.
    3. Listens for JSON command messages (``action``, ``perceive``, ``ping``).
    4. Delegates execution to :class:`WebExecutor`.
    5. Forwards UI interrupt events from :class:`MutationShield` to the daemon.
    """

    def __init__(
        self,
        session_id: str,
        headless: bool = False,
        target_url: Optional[str] = None,
    ) -> None:
        self._session_id = session_id
        self._headless = headless
        self._target_url = target_url

        # Playwright objects (initialised in start()).
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None

        # Bridge components.
        self._executor: Optional[WebExecutor] = None
        self._shield: Optional[MutationShield] = None

        # WebSocket state.
        self._ws = None
        self._connected: bool = False
        self._running: bool = False

        # Background tasks.
        self._interrupt_forwarder_task: Optional[asyncio.Task] = None

        # Priority control: gate pauses interrupt forwarding during actions.
        self._action_gate: asyncio.Event = asyncio.Event()
        self._action_gate.set()  # Open by default (no action running).

        # WebSocket send lock to prevent interleaved writes.
        self._ws_lock: asyncio.Lock = asyncio.Lock()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    async def start(self, daemon_url: str = _DEFAULT_DAEMON_URL) -> None:
        """Connect to the daemon and enter the main loop.

        The Playwright browser is NOT launched here — it starts lazily on
        the first ``navigate`` or ``perceive`` command.  This prevents a
        spurious browser window from opening when the agent only needs
        desktop automation.

        This method runs until :meth:`stop` is called or the process is
        interrupted.
        """
        self._running = True
        logger.info(
            "WebBridgeWorker: starting (session=%s, headless=%s, lazy-browser)",
            self._session_id, self._headless,
        )

        try:
            await self._connect_loop(daemon_url)
        except asyncio.CancelledError:
            logger.info("WebBridgeWorker: cancelled.")
        except Exception as exc:
            logger.error("WebBridgeWorker: fatal error: %s", exc, exc_info=True)
        finally:
            await self.stop()

    async def _ensure_browser(self) -> None:
        """Launch the browser on demand (first web command).

        Idempotent — safe to call multiple times.
        """
        if self._browser is not None:
            return
        logger.info("WebBridgeWorker: launching browser on demand...")
        await self._launch_browser()

    async def stop(self) -> None:
        """Shut down the worker, close the browser, and disconnect."""
        self._running = False
        self._connected = False

        # Cancel interrupt forwarder.
        if self._interrupt_forwarder_task and not self._interrupt_forwarder_task.done():
            self._interrupt_forwarder_task.cancel()
            try:
                await self._interrupt_forwarder_task
            except asyncio.CancelledError:
                pass

        # Teardown mutation shield.
        if self._shield:
            await self._shield.teardown()
            self._shield = None

        # Close WebSocket.
        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        # Close browser.
        if self._browser:
            try:
                await self._browser.close()
            except Exception:
                pass
            self._browser = None

        if self._playwright:
            try:
                await self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

        logger.info("WebBridgeWorker: stopped.")

    def is_connected(self) -> bool:
        """Return ``True`` if the WebSocket connection is active."""
        return self._connected

    # ------------------------------------------------------------------
    # Browser lifecycle
    # ------------------------------------------------------------------

    async def _launch_browser(self) -> None:
        """Start Playwright and open a Chromium browser with a single page."""
        self._playwright = await async_playwright().start()

        # Stealth launch args to avoid bot detection.
        launch_args = []
        if os.environ.get("OPENCLAW_WEB_STEALTH", "1") != "0":
            launch_args.append("--disable-blink-features=AutomationControlled")

        self._browser = await self._playwright.chromium.launch(
            headless=self._headless,
            args=launch_args,
        )

        # Derive dynamic user-agent from actual browser version.
        browser_version = self._browser.version
        user_agent = (
            f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            f"AppleWebKit/537.36 (KHTML, like Gecko) "
            f"Chrome/{browser_version} Safari/537.36 OpenClaw/2.0"
        )

        self._context = await self._browser.new_context(
            viewport={"width": 1280, "height": 900},
            user_agent=user_agent,
        )
        self._page = await self._context.new_page()

        # Inject anti-webdriver script to hide automation fingerprint.
        if os.environ.get("OPENCLAW_WEB_STEALTH", "1") != "0":
            await self._page.add_init_script(
                "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
            )

        if self._target_url:
            logger.info("WebBridgeWorker: navigating to %s", self._target_url)
            await self._page.goto(self._target_url, wait_until="domcontentloaded", timeout=30000)

        # Wire up executor and shield.
        self._executor = WebExecutor(self._page, self._session_id)
        self._shield = MutationShield(self._session_id)
        await self._shield.setup(self._page)

        logger.info(
            "WebBridgeWorker: browser launched and ready (version=%s).",
            browser_version,
        )

    # ------------------------------------------------------------------
    # WebSocket connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self, daemon_url: str) -> None:
        """Connect to the daemon with automatic reconnection using exponential backoff."""
        attempt = 0
        _health_url = daemon_url.replace("ws://", "http://").split("/ws/")[0] + "/health"

        while self._running and attempt < _MAX_RECONNECT_ATTEMPTS:
            # Before reconnecting, check if the daemon is reachable.
            if attempt > 0:
                delay = min(_RECONNECT_BASE_S * (2 ** (attempt - 1)), _RECONNECT_MAX_S)
                daemon_alive = await self._check_daemon_health(_health_url)
                if not daemon_alive:
                    logger.info(
                        "WebBridgeWorker: daemon unreachable, waiting %.1fs before retry...",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                logger.info("WebBridgeWorker: daemon reachable, reconnecting in %.1fs...", delay)
                await asyncio.sleep(delay)

            try:
                logger.info(
                    "WebBridgeWorker: connecting to %s (attempt %d)...",
                    daemon_url, attempt + 1,
                )
                async with ws_connect(daemon_url) as ws:
                    self._ws = ws
                    self._connected = True
                    attempt = 0  # Reset on successful connection.
                    logger.info("WebBridgeWorker: successfully connected to the daemon!")

                    # Send registration handshake.
                    await self._send_json({
                        "type": "register",
                        "bridge_type": "web",
                        "session_id": self._session_id,
                    })

                    # Start interrupt forwarder.
                    self._interrupt_forwarder_task = asyncio.create_task(
                        self._forward_interrupts(),
                        name=f"interrupt-fwd-{self._session_id}",
                    )

                    # Run message loop and keepalive ping in parallel.
                    ping_task = asyncio.create_task(self._keepalive_ping())
                    try:
                        await self._message_loop()
                    finally:
                        ping_task.cancel()
                        try:
                            await ping_task
                        except asyncio.CancelledError:
                            pass

            except ConnectionClosed as exc:
                self._connected = False
                logger.warning(
                    "WebBridgeWorker: WebSocket closed (code=%s, reason=%s).",
                    getattr(exc, "code", "?"), getattr(exc, "reason", "?"),
                )
            except OSError as exc:
                self._connected = False
                logger.warning("WebBridgeWorker: connection error: %s", exc)
            except Exception as exc:
                self._connected = False
                logger.error("WebBridgeWorker: unexpected error: %s", exc, exc_info=True)

            if self._running:
                attempt += 1

        if attempt >= _MAX_RECONNECT_ATTEMPTS:
            logger.error(
                "WebBridgeWorker: max reconnection attempts (%d) exceeded.",
                _MAX_RECONNECT_ATTEMPTS,
            )

    async def _check_daemon_health(self, health_url: str) -> bool:
        """Return True if the daemon HTTP health endpoint responds."""
        import urllib.request
        try:
            req = urllib.request.Request(health_url, method="GET")
            with urllib.request.urlopen(req, timeout=3) as resp:
                return resp.status == 200
        except Exception:
            return False

    async def _keepalive_ping(self) -> None:
        """Periodically send a ping message to keep the WebSocket alive."""
        while self._running and self._connected:
            try:
                await asyncio.sleep(_PING_INTERVAL_S)
                if self._ws and self._connected:
                    await self._send_json({"type": "ping", "session_id": self._session_id})
            except asyncio.CancelledError:
                break
            except Exception:
                break

    # ------------------------------------------------------------------
    # Message loop
    # ------------------------------------------------------------------

    async def _message_loop(self) -> None:
        """Listen for commands from the daemon and dispatch them."""
        async for raw_message in self._ws:
            if not self._running:
                break

            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("WebBridgeWorker: received non-JSON message, ignoring.")
                continue

            msg_type = msg.get("type", "")
            logger.debug("WebBridgeWorker: received message type=%s", msg_type)

            if msg_type == "action":
                await self._handle_action(msg)
            elif msg_type == "perceive":
                await self._handle_perceive(msg)
            elif msg_type == "navigate":
                await self._handle_navigate(msg)
            elif msg_type == "screenshot":
                await self._handle_screenshot(msg)
            elif msg_type == "rollback":
                await self._handle_rollback(msg)
            elif msg_type == "ping":
                await self._send_json({"type": "pong", "session_id": self._session_id})
            elif msg_type == "shutdown":
                logger.info("WebBridgeWorker: received shutdown command.")
                self._running = False
                break
            elif msg_type in ("error", "interrupt_ack"):
                # Server-initiated messages — log only, do not echo back.
                logger.debug(
                    "WebBridgeWorker: received server message type '%s': %s",
                    msg_type, msg.get("message", msg.get("data", "")),
                )
            else:
                logger.warning("WebBridgeWorker: unknown message type '%s'.", msg_type)

    # ------------------------------------------------------------------
    # Command handlers
    # ------------------------------------------------------------------

    async def _handle_action(self, msg: dict) -> None:
        """Parse and execute an action request with priority over interrupt forwarding."""
        await self._ensure_browser()
        data = msg.get("data", {})
        req_id = data.get("request_id")
        try:
            action = ActionRequest(**data)
        except Exception as exc:
            resp = {
                "type": "action_result",
                "session_id": self._session_id,
                "data": {"success": False, "error": f"Invalid: {exc}"},
            }
            if req_id:
                resp["request_id"] = req_id
            await self._send_json(resp)
            return

        # PAUSE interrupt forwarding while action executes and result sends.
        self._action_gate.clear()
        try:
            result = await self._executor.act(action)
            resp = {
                "type": "action_result",
                "session_id": self._session_id,
                "data": result.model_dump(),
            }
            if req_id:
                resp["request_id"] = req_id
            await self._send_json(resp)
        finally:
            # RESUME interrupt forwarding after result is sent.
            self._action_gate.set()

    async def _handle_perceive(self, msg: dict) -> None:
        """Capture current page state and return it."""
        await self._ensure_browser()
        data = msg.get("data", {})
        perceive_mode = data.get("perceive_mode", "full")
        if perceive_mode == "quick":
            perception = await self._executor.perceive_quick()
        else:
            perception = await self._executor.perceive()
        resp = {
            "type": "perception",
            "session_id": self._session_id,
            "data": perception.model_dump(),
        }
        req_id = msg.get("data", {}).get("request_id")
        if req_id:
            resp["request_id"] = req_id
        await self._send_json(resp)

    async def _handle_navigate(self, msg: dict) -> None:
        """Navigate the browser to a URL (launches browser on demand)."""
        await self._ensure_browser()
        data = msg.get("data", {})
        url = data.get("url", "")
        req_id = data.get("request_id")
        try:
            logger.info("WebBridgeWorker: navigating to %s", url)
            await self._page.goto(url, wait_until="domcontentloaded", timeout=30000)
            resp = {
                "type": "navigate_result",
                "session_id": self._session_id,
                "data": {"success": True, "url": self._page.url},
            }
            if req_id:
                resp["request_id"] = req_id
            await self._send_json(resp)
        except Exception as exc:
            logger.error("WebBridgeWorker: navigation failed: %s", exc)
            resp = {
                "type": "navigate_result",
                "session_id": self._session_id,
                "data": {"success": False, "error": str(exc)},
            }
            if req_id:
                resp["request_id"] = req_id
            await self._send_json(resp)

    async def _handle_screenshot(self, msg: dict) -> None:
        """Capture a screenshot of the current page and return as base64."""
        import base64
        try:
            png_bytes = await self._page.screenshot(full_page=False, type="png")
            b64 = base64.b64encode(png_bytes).decode("ascii")
            await self._send_json({
                "type": "screenshot_result",
                "session_id": self._session_id,
                "data": {
                    "success": True,
                    "image_b64": b64,
                    "url": self._page.url,
                    "title": await self._page.title(),
                },
            })
        except Exception as exc:
            logger.error("WebBridgeWorker: screenshot failed: %s", exc)
            await self._send_json({
                "type": "screenshot_result",
                "session_id": self._session_id,
                "data": {"success": False, "error": str(exc)},
            })

    async def _handle_rollback(self, msg: dict) -> None:
        """Attempt to undo the last action."""
        success = await self._executor.rollback()
        await self._send_json({
            "type": "rollback_result",
            "session_id": self._session_id,
            "data": {"success": success},
        })

    # ------------------------------------------------------------------
    # Interrupt forwarding
    # ------------------------------------------------------------------

    async def _forward_interrupts(self) -> None:
        """Background task: drain MutationShield events and send to daemon.

        Respects the action gate — pauses while an action is executing to
        ensure ``action_result`` is always sent before any interrupt events.
        Rate-limited to max 2 events/second to prevent WebSocket flooding.
        """
        last_send = 0.0
        while self._running and self._connected:
            try:
                # Wait until no action is executing.
                await self._action_gate.wait()

                event = await self._shield.get_event(timeout=1.0)
                if event is None:
                    continue

                # Rate limit: skip if too soon after last send.
                now = time.monotonic()
                if now - last_send < _MIN_INTERRUPT_INTERVAL_S:
                    continue

                # Double-check gate (action may have started during get_event).
                await self._action_gate.wait()

                logger.info(
                    "WebBridgeWorker: forwarding UI interrupt (%s): %s",
                    event.interrupt_type, event.description[:80],
                )
                await self._send_json({
                    "type": "ui_interrupt",
                    "session_id": self._session_id,
                    "data": event.model_dump(),
                })
                last_send = time.monotonic()
            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("WebBridgeWorker: interrupt forwarder error: %s", exc)
                await asyncio.sleep(0.5)

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    async def _send_json(self, data: dict) -> None:
        """Serialize and send a JSON message over the WebSocket.

        Protected by ``_ws_lock`` to prevent interleaved writes from
        concurrent async tasks (message loop, interrupt forwarder, ping).
        """
        async with self._ws_lock:
            if self._ws:
                try:
                    await self._ws.send(json.dumps(data, default=str, ensure_ascii=False))
                except Exception as exc:
                    logger.warning("WebBridgeWorker: failed to send message: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run the web bridge worker as a standalone process."""
    import argparse

    # Force UTF-8 on all streams to avoid GBK encoding errors on Chinese Windows.
    import io as _io, sys as _sys
    if hasattr(_sys.stderr, "buffer"):
        _sys.stderr = _io.TextIOWrapper(_sys.stderr.buffer, encoding="utf-8", errors="replace")
    if hasattr(_sys.stdout, "buffer"):
        _sys.stdout = _io.TextIOWrapper(_sys.stdout.buffer, encoding="utf-8", errors="replace")

    parser = argparse.ArgumentParser(description="OpenClaw Web Bridge Worker")
    parser.add_argument("--session-id", default="web-default", help="Session identifier")
    parser.add_argument("--daemon-url", default=_DEFAULT_DAEMON_URL, help="Daemon WebSocket URL")
    parser.add_argument("--target-url", default=None, help="Initial page URL")
    parser.add_argument("--headless", action="store_true", help="Launch browser in headless mode (default is headed)")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    worker = WebBridgeWorker(
        session_id=args.session_id,
        headless=args.headless,
        target_url=args.target_url,
    )

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))
        except NotImplementedError:
            # Windows does not support add_signal_handler.
            pass

    await worker.start(daemon_url=args.daemon_url)


if __name__ == "__main__":
    asyncio.run(main())
