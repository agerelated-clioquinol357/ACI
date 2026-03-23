"""
OpenClaw 2.0 ACI Framework - Mutation Shield.

Monitors the live Playwright page for UI interrupts that may block the
planned action flow.  Detects:

* High z-index overlay elements appearing (modals, cookie banners, etc.).
* Native dialog/alert events (``window.alert``, ``window.confirm``, ``window.prompt``).
* URL changes (unexpected navigations / redirects).

Detected events are pushed to an :class:`asyncio.Queue` so the
:mod:`worker` can forward them to the daemon as :class:`UIInterruptEvent` objects.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from playwright.async_api import Dialog, Page

from core.models.schemas import UIInterruptEvent

logger = logging.getLogger(__name__)

# JavaScript injected into the page to watch for high z-index overlay elements.
_MUTATION_OBSERVER_JS = """
(() => {
    if (window.__ocMutationShieldActive) return;
    window.__ocMutationShieldActive = true;
    window.__ocInterruptQueue = window.__ocInterruptQueue || [];

    const Z_INDEX_THRESHOLD = 1000;

    const observer = new MutationObserver((mutations) => {
        for (const mutation of mutations) {
            for (const node of mutation.addedNodes) {
                if (node.nodeType !== Node.ELEMENT_NODE) continue;
                try {
                    const style = getComputedStyle(node);
                    const z = parseInt(style.zIndex, 10);
                    if (!isNaN(z) && z > Z_INDEX_THRESHOLD) {
                        const rect = node.getBoundingClientRect();
                        // Only fire if the element is reasonably large
                        // (likely a modal, not a tooltip).
                        if (rect.width > 100 && rect.height > 50) {
                            window.__ocInterruptQueue.push({
                                type: 'overlay',
                                tagName: node.tagName.toLowerCase(),
                                zIndex: z,
                                text: (node.innerText || '').slice(0, 200),
                                width: Math.round(rect.width),
                                height: Math.round(rect.height),
                                timestamp: Date.now(),
                            });
                        }
                    }
                } catch (_) {}
            }
        }
    });

    observer.observe(document.body, { childList: true, subtree: true });
})();
"""

# JavaScript to drain queued overlay events from the page context.
_DRAIN_QUEUE_JS = """
(() => {
    const q = window.__ocInterruptQueue || [];
    window.__ocInterruptQueue = [];
    return q;
})();
"""


class MutationShield:
    """Watches a Playwright page for UI interrupts and feeds them to a queue.

    Usage::

        shield = MutationShield(session_id="my-session")
        await shield.setup(page)

        # In the worker's main loop:
        while True:
            event = await shield.get_event()   # blocks until an interrupt
            # ... forward event to daemon ...

        await shield.teardown()
    """

    def __init__(self, session_id: str) -> None:
        self._session_id: str = session_id
        self._page: Optional[Page] = None
        self._event_queue: asyncio.Queue[UIInterruptEvent] = asyncio.Queue(maxsize=50)
        self._poll_task: Optional[asyncio.Task] = None
        self._previous_url: Optional[str] = None
        self._running: bool = False
        self._frames: list = []

    # ------------------------------------------------------------------
    # Setup / Teardown
    # ------------------------------------------------------------------

    async def setup(self, page: Page) -> None:
        """Attach the mutation shield to a Playwright page.

        Injects the MutationObserver script, registers dialog handlers,
        and starts the background polling loop.
        """
        self._page = page
        self._previous_url = page.url
        self._running = True

        # Inject MutationObserver for overlay detection.
        try:
            await page.evaluate(_MUTATION_OBSERVER_JS)
        except Exception as exc:
            logger.warning("MutationShield: failed to inject observer: %s", exc)

        # Listen for native dialog events.
        page.on("dialog", self._on_dialog)

        # Start background polling for overlay events and URL changes.
        self._poll_task = asyncio.create_task(
            self._poll_loop(), name=f"mutation-shield-poll-{self._session_id}"
        )
        logger.info("MutationShield: setup complete for session '%s'.", self._session_id)

    async def setup_for_frames(self, frames: list) -> None:
        """Inject MutationObserver into same-origin iframes."""
        self._frames = frames
        for frame in frames:
            try:
                await frame.evaluate(_MUTATION_OBSERVER_JS)
                logger.debug("MutationShield: observer injected into frame %s", frame.url)
            except Exception as exc:
                logger.debug("MutationShield: failed to inject into frame: %s", exc)

    async def teardown(self) -> None:
        """Detach the shield and cancel background tasks."""
        self._running = False
        if self._poll_task and not self._poll_task.done():
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
        self._poll_task = None

        if self._page:
            try:
                self._page.remove_listener("dialog", self._on_dialog)
            except Exception:
                pass
        self._page = None
        logger.info("MutationShield: teardown complete for session '%s'.", self._session_id)

    # ------------------------------------------------------------------
    # Event retrieval
    # ------------------------------------------------------------------

    async def get_event(self, timeout: Optional[float] = None) -> Optional[UIInterruptEvent]:
        """Wait for the next UI interrupt event.

        Args:
            timeout: Maximum seconds to wait. ``None`` waits indefinitely.

        Returns:
            A :class:`UIInterruptEvent`, or ``None`` if the timeout elapsed.
        """
        try:
            return await asyncio.wait_for(self._event_queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None

    def has_pending_events(self) -> bool:
        """Return ``True`` if there are unprocessed events in the queue."""
        return not self._event_queue.empty()

    # ------------------------------------------------------------------
    # Internal: Dialog handler
    # ------------------------------------------------------------------

    def _on_dialog(self, dialog: Dialog) -> None:
        """Handle native browser dialog events (alert, confirm, prompt)."""
        event = UIInterruptEvent(
            session_id=self._session_id,
            interrupt_type="dialog",
            description=(
                f"Browser {dialog.type} dialog: "
                f"{dialog.message[:200] if dialog.message else '(empty)'}"
            ),
        )
        try:
            self._event_queue.put_nowait(event)
        except asyncio.QueueFull:
            logger.warning("MutationShield: event queue full, dropping dialog event.")

        # Auto-dismiss the dialog to prevent blocking Playwright.
        asyncio.ensure_future(self._dismiss_dialog(dialog))

    @staticmethod
    async def _dismiss_dialog(dialog: Dialog) -> None:
        """Dismiss a dialog after a brief delay for observation."""
        try:
            await asyncio.sleep(0.1)
            await dialog.dismiss()
        except Exception as exc:
            logger.debug("MutationShield: failed to dismiss dialog: %s", exc)

    # ------------------------------------------------------------------
    # Internal: Polling loop
    # ------------------------------------------------------------------

    async def _poll_loop(self) -> None:
        """Background task that polls for overlay events and URL changes."""
        while self._running:
            try:
                await asyncio.sleep(0.5)  # Poll every 500ms.

                if self._page is None or self._page.is_closed():
                    break

                # --- Check for overlay events from the injected observer ---
                try:
                    raw_events: list[dict] = await self._page.evaluate(_DRAIN_QUEUE_JS)
                except Exception:
                    raw_events = []

                # Also drain events from iframes
                for frame in self._frames:
                    try:
                        frame_events = await frame.evaluate(_DRAIN_QUEUE_JS)
                        raw_events.extend(frame_events)
                    except Exception:
                        pass

                # Batch limit: at most 3 overlay events per poll cycle.
                # On dynamic SPAs this prevents flooding the queue.
                _MAX_EVENTS_PER_POLL = 3
                queued_this_cycle = 0
                if len(raw_events) > _MAX_EVENTS_PER_POLL:
                    logger.debug(
                        "MutationShield: %d overlay events, keeping last %d",
                        len(raw_events), _MAX_EVENTS_PER_POLL,
                    )
                    raw_events = raw_events[-_MAX_EVENTS_PER_POLL:]

                for raw in raw_events:
                    event = UIInterruptEvent(
                        session_id=self._session_id,
                        interrupt_type="overlay",
                        description=(
                            f"High z-index element appeared: "
                            f"<{raw.get('tagName', '?')}> z-index={raw.get('zIndex', '?')} "
                            f"size={raw.get('width', '?')}x{raw.get('height', '?')} "
                            f"text={raw.get('text', '')[:100]!r}"
                        ),
                    )
                    try:
                        self._event_queue.put_nowait(event)
                        queued_this_cycle += 1
                    except asyncio.QueueFull:
                        logger.debug("MutationShield: event queue full, dropping overlay event.")
                        break

                # --- Check for URL changes (redirects) ---
                current_url = self._page.url
                if self._previous_url and current_url != self._previous_url:
                    event = UIInterruptEvent(
                        session_id=self._session_id,
                        interrupt_type="redirect",
                        description=(
                            f"URL changed: {self._previous_url} -> {current_url}"
                        ),
                    )
                    try:
                        self._event_queue.put_nowait(event)
                    except asyncio.QueueFull:
                        logger.warning("MutationShield: event queue full, dropping redirect event.")
                    self._previous_url = current_url

            except asyncio.CancelledError:
                break
            except Exception as exc:
                logger.debug("MutationShield: poll loop error: %s", exc)
                await asyncio.sleep(1.0)  # Back off on errors.

        logger.debug("MutationShield: poll loop exited for session '%s'.", self._session_id)
