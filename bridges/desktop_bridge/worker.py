"""
OpenClaw 2.0 ACI Framework - Desktop Bridge Worker.

WebSocket client that connects to the OpenClaw daemon as a ``desktop`` bridge.
Delegates perception to :mod:`uia_extractor`, action execution to
:mod:`physical_input`, and falls back to :mod:`vision_fallback` when the
structured UIA tree cannot locate the target.

Platform: Windows only.  On other platforms the worker logs an error and
exits gracefully.
"""

from __future__ import annotations

import asyncio
import base64
import json
import logging
import os
import pathlib
import platform
import signal
import tempfile
import time
from typing import Optional
from concurrent.futures import ThreadPoolExecutor

try:
    import websockets
    from websockets.asyncio.client import connect as ws_connect
    from websockets.exceptions import ConnectionClosed
except ImportError:
    import websockets  # type: ignore[no-redef]
    ws_connect = websockets.connect  # type: ignore[attr-defined]
    ConnectionClosed = websockets.exceptions.ConnectionClosed  # type: ignore[attr-defined]

try:
    import uiautomation as auto  # type: ignore[import-untyped]
except ImportError:
    auto = None  # type: ignore[assignment]

from core.models.schemas import (
    ActionRequest,
    ActionResult,
    ContextPerception,
    TaskState,
    UIDNode,
)

from . import uia_extractor as uia_mod
from . import physical_input as phys
from . import vision_fallback as vision
from . import app_launcher
from .desktop_shield import DesktopShield
from . import ocr_validator

from memory_core.muscle_memory import MuscleMemoryStore
from memory_core import knowledge_base as kb
from .perception_fusion import PerceptionFusion, visual_gatekeeper_check

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"
_DEFAULT_DAEMON_URL = "ws://127.0.0.1:11434/ws/bridge/desktop"
_RECONNECT_BASE_S = 2.0
_RECONNECT_MAX_S = 30.0
_MAX_RECONNECT_ATTEMPTS = 20
_PING_INTERVAL_S = 25.0

# Minimum *actionable* UIA element count before vision fallback triggers.
# "Actionable" means tags in _ACTIONABLE_TAGS — text containers (span,
# section, article, fieldset, etc.) are excluded from the count.
_VISION_THRESHOLD = int(os.environ.get("OPENCLAW_VISION_THRESHOLD", "5"))

# Tags that count as genuinely actionable for the threshold check.
# Containers like span/section/article/fieldset are excluded because
# apps like WeChat emit dozens of them via UIA even though they're not
# meaningfully interactable.
_ACTIONABLE_TAGS = {
    "button", "checkbox", "select", "input", "a", "menuitem",
    "radio", "tab", "treeitem", "li", "slider",
}

# Tags that are pure layout/text containers — not actionable.
_CONTAINER_TAGS = {
    "span", "section", "article", "fieldset", "dialog", "toolbar",
    "nav", "menu", "tree", "tablist", "ul", "table", "progress",
    "scrollbar", "img", "tooltip", "calendar", "spinner", "unknown",
}

# Minimum confidence for vision elements to be interactable.
_VISION_MIN_CONFIDENCE = float(os.environ.get("OPENCLAW_VISION_MIN_CONFIDENCE", "0.3"))

# Delay before capturing post-action verification screenshot (seconds).
_VERIFY_DELAY_S = float(os.environ.get("OPENCLAW_VERIFY_DELAY", "0.3"))

# Maximum number of element-level thumbnails per perceive cycle.
_MAX_THUMBNAILS = 15

# --- Feature flags for perception fusion (spec section 4.5) ---
_FUSION_ENABLED = os.environ.get("OPENCLAW_PERCEPTION_FUSION", "1") != "0"
_PREFER_UIA_ENABLED = os.environ.get("OPENCLAW_PREFER_UIA_PATTERNS", "1") != "0"

# Window class names / exe names that should never be the capture target.
_TERMINAL_CLASS_NAMES = {
    "ConsoleWindowClass",       # cmd.exe / legacy console
    "CASCADIA_HOSTING_HWND",    # Windows Terminal
    "PseudoConsoleWindow",      # ConPTY
    "mintty",                   # Git Bash mintty
}
_TERMINAL_EXE_SUBSTRINGS = {
    "powershell", "pwsh", "cmd.exe", "windowsterminal",
    "conhost", "mintty", "python",  # our own worker process
    "claude code",  # Claude Code CLI runs in terminal
}

# Directory for saving screenshots to files instead of base64.
_SCREENSHOT_DIR = pathlib.Path(os.environ.get(
    "OPENCLAW_SCREENSHOT_DIR",
    os.path.join(tempfile.gettempdir(), "openclaw_screenshots"),
))


def _save_screenshot_to_file(b64_data: str, prefix: str, session_id: str) -> str:
    """Save base64 screenshot to local file, return file path."""
    _SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    filename = f"{prefix}_{session_id}_{timestamp}.png"
    filepath = _SCREENSHOT_DIR / filename
    filepath.write_bytes(base64.b64decode(b64_data))
    return str(filepath)


class DesktopBridgeWorker:
    """Long-running WebSocket client that bridges the daemon to the Windows desktop.

    Lifecycle::

        worker = DesktopBridgeWorker(session_id="desktop-1")
        await worker.start()
        # ... runs until shutdown ...
        await worker.stop()
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._ws = None
        self._connected: bool = False
        self._running: bool = False

        # Declare DPI awareness before any Win32 calls so that coordinates
        # from screenshots match actual pixel positions (no bitmap stretching).
        if _IS_WINDOWS:
            try:
                import ctypes
                # Per-Monitor V2 (Win10 1703+)
                ctypes.windll.shcore.SetProcessDpiAwareness(2)
            except Exception:
                try:
                    import ctypes
                    ctypes.windll.user32.SetProcessDPIAware()
                except Exception:
                    pass

        # UIA extractor instance.
        self._extractor: Optional[uia_mod.UIAExtractor] = None

        # Track last action for rollback.
        self._last_action: Optional[ActionRequest] = None

        # Cached element map: uid -> UIDNode (refreshed on each perceive).
        self._element_map: dict[str, UIDNode] = {}

        # Perception fusion instance.
        self._fusion = PerceptionFusion()

        # Vision element cache: vc_N -> element dict with bbox/confidence.
        # Cleared and repopulated on each perceive that triggers vision mode.
        self._vision_element_cache: dict[str, dict] = {}

        # Window offset: (left, top) of the foreground window in screen coords.
        # Vision bboxes are window-relative; clicks need screen-absolute coords.
        self._window_offset: tuple[int, int] = (0, 0)

        # Desktop interrupt detection shield.
        self._desktop_shield = DesktopShield(session_id)

        # Unified muscle memory for caching vc_ element crops.
        self._muscle_memory = MuscleMemoryStore()

        # Pluggable detection tier registry (cursor probe → OCR → contour → VLM).
        self._tier_registry = self._init_tier_registry()

        # Target application HWND — set during perceive or launch_app,
        # reused for screenshots and actions so we don't accidentally
        # capture PowerShell or other terminal windows.
        self._target_hwnd: Optional[int] = None

        # Target app identity — class name and title pattern recorded
        # after launch_app or first successful perceive.  Used to
        # re-find the window even when it loses focus.
        self._target_class_name: Optional[str] = None
        self._target_title_pattern: Optional[str] = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def _init_tier_registry(self):
        """Initialize the pluggable detection tier registry."""
        from core.detection_tier import TierRegistry

        registry = TierRegistry()

        try:
            from .cursor_probe import CursorProbe
            registry.register(CursorProbe(
                target_hwnd_getter=lambda: self._target_hwnd,
            ))
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: CursorProbe unavailable: %s", exc)

        try:
            from .fast_ocr import FastOCR
            registry.register(FastOCR())
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: FastOCR unavailable: %s", exc)

        try:
            from .contour_detector import ContourDetector
            registry.register(ContourDetector())
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: ContourDetector unavailable: %s", exc)

        try:
            from .vlm_identifier import VLMIdentifier
            registry.register(VLMIdentifier())
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: VLMIdentifier unavailable: %s", exc)

        return registry

    async def start(self, daemon_url: str = _DEFAULT_DAEMON_URL) -> None:
        """Connect to the daemon and enter the main loop."""
        if not _IS_WINDOWS:
            logger.error(
                "DesktopBridgeWorker: this bridge requires Windows. "
                "Current platform: %s. Exiting.",
                platform.system(),
            )
            return

        self._running = True
        try:
            self._extractor = uia_mod.UIAExtractor()
        except Exception as exc:
            logger.error("DesktopBridgeWorker: failed to initialise UIA extractor: %s", exc)
            return

        logger.info("DesktopBridgeWorker: starting (session=%s)", self._session_id)

        try:
            await self._connect_loop(daemon_url)
        except asyncio.CancelledError:
            logger.info("DesktopBridgeWorker: cancelled.")
        except Exception as exc:
            logger.error("DesktopBridgeWorker: fatal error: %s", exc, exc_info=True)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Shut down the worker and disconnect."""
        self._running = False
        self._connected = False

        if self._ws:
            try:
                await self._ws.close()
            except Exception:
                pass
            self._ws = None

        logger.info("DesktopBridgeWorker: stopped.")

    def is_connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------
    # Connection loop
    # ------------------------------------------------------------------

    async def _connect_loop(self, daemon_url: str) -> None:
        """Connect with automatic reconnection using exponential backoff."""
        attempt = 0
        # Derive the HTTP health URL from the WebSocket URL.
        _health_url = daemon_url.replace("ws://", "http://").split("/ws/")[0] + "/health"

        while self._running and attempt < _MAX_RECONNECT_ATTEMPTS:
            # Before reconnecting, check if the daemon is reachable.
            if attempt > 0:
                delay = min(_RECONNECT_BASE_S * (2 ** (attempt - 1)), _RECONNECT_MAX_S)
                daemon_alive = await self._check_daemon_health(_health_url)
                if not daemon_alive:
                    logger.info(
                        "DesktopBridgeWorker: daemon unreachable, waiting %.1fs before retry...",
                        delay,
                    )
                    await asyncio.sleep(delay)
                    attempt += 1
                    continue
                logger.info("DesktopBridgeWorker: daemon reachable, reconnecting in %.1fs...", delay)
                await asyncio.sleep(delay)

            try:
                logger.info(
                    "DesktopBridgeWorker: connecting to %s (attempt %d)...",
                    daemon_url, attempt + 1,
                )
                async with ws_connect(daemon_url) as ws:
                    self._ws = ws
                    self._connected = True
                    attempt = 0
                    logger.info("DesktopBridgeWorker: successfully connected to the daemon!")

                    await self._send_json({
                        "type": "register",
                        "bridge_type": "desktop",
                        "session_id": self._session_id,
                    })

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
                    "DesktopBridgeWorker: WebSocket closed (code=%s, reason=%s).",
                    getattr(exc, "code", "?"), getattr(exc, "reason", "?"),
                )
            except OSError as exc:
                self._connected = False
                logger.warning("DesktopBridgeWorker: connection error: %s", exc)
            except Exception as exc:
                self._connected = False
                logger.error("DesktopBridgeWorker: unexpected error: %s", exc, exc_info=True)

            if self._running:
                attempt += 1

        if attempt >= _MAX_RECONNECT_ATTEMPTS:
            logger.error("DesktopBridgeWorker: max reconnection attempts exceeded.")

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
        """Listen for commands from the daemon."""
        async for raw_message in self._ws:
            if not self._running:
                break

            try:
                msg = json.loads(raw_message)
            except json.JSONDecodeError:
                logger.warning("DesktopBridgeWorker: non-JSON message, ignoring.")
                continue

            msg_type = msg.get("type", "")

            if msg_type == "action":
                await self._handle_action(msg)
            elif msg_type == "perceive":
                await self._handle_perceive(msg)
            elif msg_type == "screenshot":
                await self._handle_screenshot(msg)
            elif msg_type == "rollback":
                await self._handle_rollback(msg)
            elif msg_type == "ping":
                await self._send_json({"type": "pong", "session_id": self._session_id})
            elif msg_type == "shutdown":
                logger.info("DesktopBridgeWorker: received shutdown command.")
                self._running = False
                break
            elif msg_type in ("error", "interrupt_ack"):
                # Server-initiated messages — log only, do not echo back.
                logger.debug(
                    "DesktopBridgeWorker: received server message type '%s'.",
                    msg_type,
                )
            else:
                logger.warning("DesktopBridgeWorker: unknown message type '%s'.", msg_type)

    # ------------------------------------------------------------------
    # Perception
    # ------------------------------------------------------------------

    async def _handle_perceive(self, msg: dict) -> None:
        """Extract the UIA tree and return a ContextPerception.

        If UIA returns too few elements, triggers annotated vision mode to
        detect elements via VLM and return an annotated screenshot.
        """
        data = msg.get("data", {})
        region = data.get("region")  # Optional [x, y, w, h] ROI
        req_id = data.get("request_id")

        perception = await asyncio.get_event_loop().run_in_executor(
            None, self._perceive_sync, region
        )
        resp = {
            "type": "perception",
            "session_id": self._session_id,
            "data": perception.model_dump(),
        }
        if req_id:
            resp["request_id"] = req_id
        await self._send_json(resp)

    def _perceive_sync(self, region: Optional[list[int]] = None) -> ContextPerception:
        """Synchronous perception (runs in thread pool).

        COM must be initialised per-thread for ``uiautomation`` to work.

        Decision logic (quality-based, not raw count):
            1. Extract UIA tree.
            2. Count *actionable* elements (buttons, inputs, etc.) — text
               containers (span, section, article) do NOT count.
            3. If actionable count <= _VISION_THRESHOLD  →  run full tier
               waterfall (CursorProbe + FastOCR + ContourDetector + VLM)
               and return annotated screenshot.
            4. Otherwise  →  return UIA tree with spatial context that
               includes pattern hints for agent common-sense reasoning.

        In BOTH cases, app knowledge from the YAML knowledge base is
        loaded and returned so the agent can use shortcuts/UI patterns.
        """
        import ctypes
        ctypes.windll.ole32.CoInitialize(0)
        try:
            # Resolve and focus the target window BEFORE extracting UIA.
            self._update_target_hwnd()
            if not self._ensure_target_focused():
                self._force_foreground()
                time.sleep(0.1)

            title = self._extractor.get_window_title() if self._extractor else ""

            # Use UIA's resolved HWND as the target if available.
            uia_hwnd = self._extractor.get_last_hwnd() if self._extractor else None
            if uia_hwnd and not self._is_terminal_window(uia_hwnd):
                self._target_hwnd = uia_hwnd
                self._record_window_identity(uia_hwnd)

            # Clear vision cache on each perceive cycle.
            self._vision_element_cache.clear()

            # Load YAML app knowledge for the current application.
            app_name = self._resolve_app_name()
            app_knowledge = self._load_app_knowledge(app_name, title)

            # Capture screenshot once — shared between both threads.
            screenshot_bytes = self._capture_screenshot_bytes()

            # Initialize shared variables used by both paths.
            annotated_b64: Optional[str] = None
            interrupted: Optional[str] = None

            if _FUSION_ENABLED and screenshot_bytes:
                # === PARALLEL UIA + VISION ===
                uia_nodes: list[UIDNode] = []
                vision_nodes: list[UIDNode] = []

                with ThreadPoolExecutor(max_workers=2, thread_name_prefix="perceive") as pool:
                    uia_future = pool.submit(self._extract_uia_with_com_init)
                    vision_future = pool.submit(
                        self._run_vision_waterfall_thread, screenshot_bytes, region,
                    )

                    try:
                        uia_nodes = uia_future.result(timeout=5.0)
                    except Exception as e:
                        logger.warning("DesktopBridgeWorker: UIA extraction failed/timed out: %s", e)

                    try:
                        vision_result = vision_future.result(timeout=10.0)
                        if vision_result:
                            vision_nodes, annotated_b64, interrupted = vision_result
                    except Exception as e:
                        logger.warning("DesktopBridgeWorker: Vision waterfall failed/timed out: %s", e)

                # === NORMALIZE VISION COORDS to screen-absolute ===
                # Vision tiers produce window-relative bboxes; UIA bboxes are
                # already screen-absolute.  Offset vision bboxes here so the
                # fusion layer (and all downstream code) works in a single
                # coordinate space.
                if vision_nodes and self._window_offset != (0, 0):
                    ox, oy = self._window_offset
                    vision_nodes = [
                        UIDNode(
                            uid=n.uid, tag=n.tag, role=n.role, text=n.text,
                            bbox=(n.bbox[0] + ox, n.bbox[1] + oy, n.bbox[2], n.bbox[3]) if n.bbox else None,
                            interactable=n.interactable, attributes=n.attributes, tier=n.tier,
                        )
                        for n in vision_nodes
                    ]
                    # Update vision cache with screen-absolute bboxes.
                    for n in vision_nodes:
                        if n.uid in self._vision_element_cache and n.bbox:
                            self._vision_element_cache[n.uid]["bbox"] = list(n.bbox)

                # Update element map with raw UIA nodes.
                self._element_map = {node.uid: node for node in uia_nodes}

                # === SPATIAL FUSION ===
                merged_nodes = self._fusion.merge(uia_nodes, vision_nodes)
                self._element_map.update({node.uid: node for node in merged_nodes})

                # Inject pseudo-UIA nodes if both returned nothing.
                if not merged_nodes and app_name:
                    window_size = self._get_window_size()
                    pseudo = self._load_pseudo_nodes(app_name, window_size)
                    if pseudo:
                        merged_nodes = pseudo
                        self._element_map.update({n.uid: n for n in pseudo})

                if not merged_nodes:
                    interrupted = "Both UIA and vision perception returned no elements."

                # === PSEUDO-UIA PERSISTENCE ===
                vision_unique = sum(1 for n in merged_nodes if n.uid.startswith("vc_"))
                if vision_unique >= 3 and app_name:
                    window_size = self._get_window_size()
                    node_dicts = [
                        {
                            "uid": n.uid, "tag": n.tag, "text": n.text,
                            "bbox": list(n.bbox) if n.bbox else [],
                            "interactable": n.interactable,
                            "tier": n.tier or "",
                            "confidence": float(n.attributes.get("confidence", 0.5)),
                            "cursor_type": n.attributes.get("cursor_type", ""),
                        }
                        for n in merged_nodes if n.bbox
                    ]
                    kb.save_pseudo_uia_tree(app_name, node_dicts, window_size, screenshot_bytes)

                nodes = merged_nodes

                logger.info(
                    "DesktopBridgeWorker: fusion complete — %d UIA + %d vision → %d merged nodes.",
                    len(uia_nodes), len(vision_nodes), len(nodes),
                )

            else:
                # === LEGACY PATH (fusion disabled or no screenshot) ===
                nodes = self._extractor.extract()
                self._element_map = {node.uid: node for node in nodes}

                actionable_count = sum(
                    1 for n in nodes if n.tag in _ACTIONABLE_TAGS and n.interactable
                )
                is_blackbox = app_name in ('wechat', 'weixin', 'qq', 'dingtalk')
                if actionable_count <= _VISION_THRESHOLD or is_blackbox:
                    vision_result = self._vision_perceive_sync(region)
                    if vision_result:
                        v_nodes, annotated_b64, interrupted = vision_result
                        nodes = list(nodes) + v_nodes
                        self._element_map.update({n.uid: n for n in v_nodes})

                if not nodes and app_name:
                    window_size = self._get_window_size()
                    pseudo = self._load_pseudo_nodes(app_name, window_size)
                    if pseudo:
                        nodes = pseudo

            spatial = self._build_spatial_context(nodes)
            spatial = self._inject_knowledge_hints(spatial, app_knowledge)
            return ContextPerception(
                state=TaskState.IDLE,
                session_id=self._session_id,
                active_window_title=title,
                context_env="desktop",
                elements=nodes,
                visual_reference_image=annotated_b64 if _FUSION_ENABLED else None,
                app_knowledge=app_knowledge,
                spatial_context=spatial,
                interrupted_reason=interrupted if _FUSION_ENABLED else None,
            )
        except Exception as exc:
            safe_msg = repr(exc)
            logger.error("DesktopBridgeWorker: perception failed: %s", safe_msg)
            return ContextPerception(
                state=TaskState.FAILED,
                session_id=self._session_id,
                context_env="desktop",
                interrupted_reason=f"Perception failed: {safe_msg}",
            )

    def _load_app_knowledge(
        self, app_name: Optional[str], title: str,
    ) -> Optional[dict]:
        """Load YAML knowledge, trying multiple resolution strategies.

        1. Exact app_name from process/class/title heuristic.
        2. Fuzzy match against title words (e.g. "微信" in title → wechat).
        3. If nothing matches, return None — the agent decides.
        """
        if app_name:
            try:
                data = kb.load(app_name)
                if data and (data.get("shortcuts") or data.get("elements") or data.get("app")):
                    logger.info(
                        "DesktopBridgeWorker: loaded knowledge for %r (shortcuts=%d)",
                        app_name, len(data.get("shortcuts", {})),
                    )
                    return data
            except Exception as exc:
                logger.warning("DesktopBridgeWorker: knowledge load failed for %r: %r", app_name, exc)

        # Fallback: try each word in the window title as a fuzzy key.
        if title:
            for word in title.replace(" - ", " ").split():
                word = word.strip().lower()
                if len(word) < 2:
                    continue
                try:
                    data = kb.load(word)
                    if data and data.get("app"):
                        logger.info(
                            "DesktopBridgeWorker: fuzzy knowledge match via title word %r → app=%r",
                            word, data.get("app"),
                        )
                        return data
                except Exception:
                    pass

        logger.info(
            "DesktopBridgeWorker: no knowledge found for app_name=%r title=%r",
            app_name, title[:50] if title else "",
        )
        return None

    def _get_window_size(self) -> tuple[int, int]:
        """Get current target window size."""
        if self._target_hwnd and _IS_WINDOWS:
            try:
                import ctypes.wintypes
                rect_s = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(
                    self._target_hwnd, ctypes.byref(rect_s),
                )
                return (
                    max(rect_s.right - rect_s.left, 1),
                    max(rect_s.bottom - rect_s.top, 1),
                )
            except Exception:
                pass
        return (1920, 1080)

    def _load_pseudo_nodes(self, app_name: str, window_size: tuple[int, int]) -> list[UIDNode]:
        """Load pseudo-UIA nodes from knowledge cache."""
        pseudo_nodes: list[UIDNode] = []
        for pk in kb.load_pseudo_uia_tree(app_name, window_size):
            bbox = tuple(pk["bbox"]) if pk.get("bbox") else None
            pk_attrs: dict[str, str] = dict(pk.get("attributes") or {})
            pk_attrs["confidence"] = str(pk["confidence"])
            pseudo_nodes.append(UIDNode(
                uid=pk["uid"], tag=pk["tag"],
                role=pk.get("tier", "knowledge"), text=pk["text"],
                bbox=bbox, attributes=pk_attrs,
                interactable=True, tier="knowledge",
            ))
        if pseudo_nodes:
            logger.info(
                "DesktopBridgeWorker: loaded %d pseudo-UIA nodes from knowledge cache.",
                len(pseudo_nodes),
            )
        return pseudo_nodes

    def _resolve_app_name(self) -> Optional[str]:
        """Identify the current app using process name, window class, or title heuristic.

        Priority chain:
            1. HWND → PID → process name → kb.find_by_process_name()
            2. window class name → kb.find_by_window_class()
            3. window title heuristic: title.split(" - ")[-1].strip().lower()
        """
        # 1. Try process name via HWND → PID.
        if self._target_hwnd and _IS_WINDOWS:
            try:
                import ctypes
                import ctypes.wintypes
                pid = ctypes.wintypes.DWORD()
                ctypes.windll.user32.GetWindowThreadProcessId(
                    self._target_hwnd, ctypes.byref(pid),
                )
                if pid.value:
                    import psutil
                    proc = psutil.Process(pid.value)
                    proc_name = proc.name()
                    logger.info("_resolve_app_name: process=%r pid=%d", proc_name, pid.value)
                    app = kb.find_by_process_name(proc_name)
                    if app:
                        logger.info("_resolve_app_name: resolved via process → %r", app)
                        return app
            except ImportError:
                logger.warning("_resolve_app_name: psutil not installed — process-based resolution unavailable")
            except Exception as exc:
                logger.debug("_resolve_app_name: process lookup failed: %r", exc)

        # 2. Try window class name.
        if self._target_class_name:
            logger.debug("_resolve_app_name: trying class=%r", self._target_class_name)
            app = kb.find_by_window_class(self._target_class_name)
            if app:
                logger.info("_resolve_app_name: resolved via window class → %r", app)
                return app

        # 3. Title heuristic fallback.
        title = self._extractor.get_window_title() if self._extractor else ""
        if title:
            result = title.split(" - ")[-1].strip().lower()
            logger.info("_resolve_app_name: falling back to title heuristic → %r (title=%r)", result, title[:50])
            return result

        logger.info("_resolve_app_name: unable to identify app (hwnd=%s, class=%s)", self._target_hwnd, self._target_class_name)
        return None

    def _build_spatial_context(self, elements: list[UIDNode]) -> Optional[str]:
        """Generate a human-readable spatial layout description of UI elements.

        Designed to give the agent enough information for common-sense
        reasoning about UI structure.  For example:

        - "窗口右上角有3个小按钮(可能是最小化/最大化/关闭)"
        - "左侧窄列有多个图标按钮(可能是导航/功能菜单)"
        - "底部有一个宽输入框(可能是消息输入区域)"

        Groups elements by approximate rows, adds window-relative position
        labels (top-right, left-sidebar, bottom-bar), and inserts pattern
        hints that help the agent infer function from spatial layout.
        """
        if not elements:
            return None

        # Filter elements with bboxes.
        with_bbox = [(e, e.bbox) for e in elements if e.bbox and len(e.bbox) >= 4]
        if not with_bbox:
            return None

        # --- Cursor type alerts (surfaced at top of spatial context) ---
        cursor_alerts: list[str] = []
        for elem, bbox in with_bbox:
            ctype = elem.attributes.get("cursor_type", "") if elem.attributes else ""
            if ctype == "IBEAM":
                cx, cy = bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2
                cursor_alerts.append(
                    f"[Cursor Alert] I-Beam 光标在 ({cx}, {cy})，"
                    f"高度疑似输入框 uid={elem.uid}"
                )
            elif ctype == "HAND":
                cx, cy = bbox[0] + bbox[2] // 2, bbox[1] + bbox[3] // 2
                cursor_alerts.append(
                    f"[Cursor Alert] Hand 光标在 ({cx}, {cy})，"
                    f"高度疑似可点击链接/按钮 uid={elem.uid}"
                )

        # Determine window bounds from all element bboxes.
        all_x = [b[0] for _, b in with_bbox]
        all_y = [b[1] for _, b in with_bbox]
        all_r = [b[0] + b[2] for _, b in with_bbox]
        all_b = [b[1] + b[3] for _, b in with_bbox]
        win_left = min(all_x)
        win_top = min(all_y)
        win_right = max(all_r)
        win_bottom = max(all_b)
        img_w = max(win_right - win_left, 1)
        img_h = max(win_bottom - win_top, 1)

        def _zone(x: int, y: int, w: int, h: int) -> str:
            """Classify element position relative to the window."""
            cx = x + w / 2 - win_left
            cy = y + h / 2 - win_top
            h_pos = "左" if cx < img_w * 0.25 else ("右" if cx > img_w * 0.75 else "中")
            v_pos = "上" if cy < img_h * 0.2 else ("下" if cy > img_h * 0.8 else "中")
            return f"{v_pos}{h_pos}"

        # Group by Y-band (elements within 30px of each other are in the same row).
        ROW_THRESHOLD = 30
        sorted_by_y = sorted(with_bbox, key=lambda t: t[1][1])

        rows: list[list[tuple[UIDNode, tuple]]] = []
        current_row: list[tuple[UIDNode, tuple]] = [sorted_by_y[0]]
        current_y = sorted_by_y[0][1][1]

        for elem, bbox in sorted_by_y[1:]:
            if abs(bbox[1] - current_y) <= ROW_THRESHOLD:
                current_row.append((elem, bbox))
            else:
                rows.append(current_row)
                current_row = [(elem, bbox)]
                current_y = bbox[1]
        rows.append(current_row)

        # Build description.
        lines: list[str] = []
        pattern_hints: list[str] = []

        for row_idx, row in enumerate(rows):
            # Sort row by X.
            row.sort(key=lambda t: t[1][0])
            avg_y = sum(b[1] for _, b in row) // len(row)

            # Vertical position label.
            rel_y = (avg_y - win_top) / img_h
            if rel_y < 0.15:
                pos = "顶部"
            elif rel_y > 0.85:
                pos = "底部"
            elif rel_y < 0.4:
                pos = "上方"
            elif rel_y > 0.6:
                pos = "下方"
            else:
                pos = "中部"

            # Count interactable vs text elements.
            interactable = [e for e, _ in row if e.interactable]
            labeled = [e for e, _ in row if e.text and e.text != e.tag]

            parts = [f"{pos}(y≈{avg_y}): {len(row)}个元素"]

            if interactable:
                tags = {}
                for e in interactable:
                    tags[e.tag] = tags.get(e.tag, 0) + 1
                tag_desc = ", ".join(f"{c}个{t}" for t, c in tags.items())
                parts.append(f"可交互: {tag_desc}")

            # Mention labeled anchors.
            anchor_texts = [e.text for e in labeled if len(e.text) <= 30][:3]
            if anchor_texts:
                parts.append(f"文本: {', '.join(repr(t) for t in anchor_texts)}")

            # Zone classification for each element in the row.
            zones = set(_zone(*b) for _, b in row)
            if zones:
                parts.append(f"区域: {','.join(sorted(zones))}")

            # Detect consecutive same-type interactable groups.
            if len(interactable) >= 3:
                consecutive_tags = [e.tag for e in interactable]
                if len(set(consecutive_tags)) == 1:
                    first_x = row[0][1][0]
                    last_x = row[-1][1][0]
                    span = last_x - first_x
                    parts.append(
                        f"{len(interactable)}个{consecutive_tags[0]}横排(跨度{span}px)"
                    )

            lines.append(" | ".join(parts))

            # ----- Common-sense pattern hints -----
            # Pattern: 2-3 small buttons at top-right corner → window controls
            if rel_y < 0.1:
                right_buttons = [
                    (e, b) for e, b in row
                    if e.tag == "button"
                    and e.interactable
                    and (b[0] + b[2] / 2 - win_left) > img_w * 0.8
                    and b[2] < 60 and b[3] < 60
                ]
                if 2 <= len(right_buttons) <= 4:
                    pattern_hints.append(
                        f"💡 窗口右上角有{len(right_buttons)}个小按钮"
                        f"(常见模式: 最小化/最大化/关闭)"
                    )

            # Pattern: wide input box at bottom → message/search input
            for e, b in row:
                if e.tag == "input" and b[2] > img_w * 0.4 and rel_y > 0.7:
                    pattern_hints.append(
                        f"💡 底部有宽输入框 uid={e.uid}"
                        f"(常见模式: 消息输入区域或搜索框)"
                    )

            # Pattern: narrow vertical column on the left → navigation sidebar
            if len(row) >= 3:
                left_col = [
                    (e, b) for e, b in row
                    if (b[0] - win_left) < img_w * 0.15
                ]
                if len(left_col) >= 3:
                    pattern_hints.append(
                        f"💡 左侧窄列有{len(left_col)}个元素"
                        f"(常见模式: 导航侧栏或功能菜单)"
                    )

            # Pattern: search icon or magnifying glass near input
            for e, b in row:
                if e.tag == "button" and e.interactable:
                    text_lower = (e.text or "").lower()
                    if any(kw in text_lower for kw in ("搜索", "search", "🔍")):
                        pattern_hints.append(
                            f"💡 搜索按钮 uid={e.uid} 文本={e.text!r}"
                        )

        result_parts: list[str] = []
        if cursor_alerts:
            result_parts.append("=== 光标探测结果 ===")
            result_parts.extend(cursor_alerts)
            result_parts.append("")
        result_parts.extend(lines)
        if pattern_hints:
            result_parts.append("")
            result_parts.append("=== 空间模式推断 ===")
            result_parts.extend(pattern_hints)

        return "\n".join(result_parts) if result_parts else None

    @staticmethod
    def _inject_knowledge_hints(
        spatial: Optional[str], app_knowledge: Optional[dict],
    ) -> Optional[str]:
        """Append actionable hints from YAML knowledge into spatial context.

        Surfaces shortcuts and element hints so the agent sees them
        directly in the spatial description rather than having to parse
        the raw ``app_knowledge`` dict.
        """
        if not app_knowledge:
            return spatial

        knowledge_hints: list[str] = []

        shortcuts = app_knowledge.get("shortcuts", {})
        if shortcuts:
            top_shortcuts = list(shortcuts.items())[:5]
            hint_lines = [f"  {k}: {v}" for k, v in top_shortcuts]
            knowledge_hints.append(
                "=== 应用快捷键提示 (来自知识库) ===\n" + "\n".join(hint_lines)
            )

        elements_info = app_knowledge.get("elements", {})
        if elements_info:
            elem_lines = []
            for name, info in list(elements_info.items())[:5]:
                desc = info if isinstance(info, str) else info.get("description", str(info))
                elem_lines.append(f"  {name}: {desc}")
            if elem_lines:
                knowledge_hints.append(
                    "=== 已知UI元素 (来自知识库) ===\n" + "\n".join(elem_lines)
                )

        if not knowledge_hints:
            return spatial

        suffix = "\n\n" + "\n".join(knowledge_hints)
        if spatial:
            return spatial + suffix
        return suffix.strip()

    def _vision_perceive_sync(
        self, region: Optional[list[int]] = None,
        screenshot_bytes: Optional[bytes] = None,
    ) -> Optional[tuple[list[UIDNode], str, Optional[str]]]:
        """Run tier-based detection, build vc_ UIDNodes.

        Uses the pluggable TierRegistry waterfall:
            1. CursorProbe — OS-level interactability
            2. FastOCR — text labels (Windows OCR / Tesseract)
            3. ContourDetector — boundaries in OCR-sparse areas
            4. VLMIdentifier — red dot annotation → external VLM (last resort)

        Returns:
            Tuple of (vision_nodes, annotated_base64, interrupted_reason)
            or None if detection fails entirely.
        """
        if screenshot_bytes is None:
            screenshot_bytes = self._capture_screenshot_bytes()
        if screenshot_bytes is None:
            return None

        # --- Scoped Perception (ROI filtering) ---
        # Limit vision processing to the target window if known.
        roi = tuple(region) if region and len(region) == 4 else None
        if not roi and self._target_hwnd and _IS_WINDOWS:
            try:
                import ctypes.wintypes
                rect_s = ctypes.wintypes.RECT()
                ctypes.windll.user32.GetWindowRect(self._target_hwnd, ctypes.byref(rect_s))
                # Only crop if the window is non-minimized and has valid area
                if rect_s.right > rect_s.left and rect_s.bottom > rect_s.top:
                    roi = (
                        rect_s.left, rect_s.top,
                        rect_s.right - rect_s.left,
                        rect_s.bottom - rect_s.top
                    )
                    logger.info("DesktopBridgeWorker: scoping vision perception to HWND %s area: %s", self._target_hwnd, roi)
            except Exception as exc:
                logger.debug("DesktopBridgeWorker: failed to resolve ROI for HWND %s: %s", self._target_hwnd, exc)

        # Build context for tiers.
        window_title = self._extractor.get_window_title() if self._extractor else ""
        window_size = (1920, 1080)

        # Run tier waterfall.
        try:
            tier_results = self._tier_registry.run_waterfall(
                screenshot_bytes, roi=roi, context=context,
            )
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: tier detection failed: %s", exc)
            return None

        # Collect all detected elements from all tiers.
        from core.detection_tier import DetectedElement
        all_detected: list[tuple[DetectedElement, str]] = []  # (element, source_tier)
        for tr in tier_results:
            for elem in tr.elements:
                all_detected.append((elem, tr.source_name))

        if not all_detected:
            logger.info("DesktopBridgeWorker: tier detection returned no elements.")
            return None

        # Build vc_ UIDNodes.
        vision_nodes: list[UIDNode] = []
        max_confidence = 0.0

        for idx, (elem, source) in enumerate(all_detected):
            uid = f"vc_{idx}"
            confidence = elem.confidence
            max_confidence = max(max_confidence, confidence)

            # Store in cache for action resolution.
            self._vision_element_cache[uid] = {
                "bbox": list(elem.bbox),
                "confidence": confidence,
                "label": elem.label,
            }

            interactable = elem.interactable and confidence >= _VISION_MIN_CONFIDENCE

            attrs: dict[str, str] = {
                "confidence": f"{confidence:.3f}",
                "source": source,
            }
            if elem.cursor_type:
                attrs["cursor_type"] = elem.cursor_type

            # Attach T2 action history if this element has been acted on before.
            if elem.label:
                ctx = self._muscle_memory.get_action_context(elem.label)
                if ctx:
                    attrs["t2_last_action"] = ctx["last_action"]
                    if ctx["last_value"]:
                        attrs["t2_last_value"] = ctx["last_value"][:60]
                    if ctx["ui_changed"] is not None:
                        attrs["t2_ui_changed"] = str(ctx["ui_changed"]).lower()
                    attrs["t2_use_count"] = str(ctx["use_count"])

            display_text = elem.label[:200] if elem.label else f"element_{idx}"

            node = UIDNode(
                uid=uid,
                tag=elem.tag or "vision",
                role=f"{source}-element",
                text=display_text,
                bbox=tuple(int(v) for v in elem.bbox[:4]) if len(elem.bbox) >= 4 else None,
                attributes=attrs,
                interactable=interactable,
                tier=source,
            )
            vision_nodes.append(node)

        # NOTE: T2 caching is intentionally NOT done here during perception.
        # We cache ONLY when an action succeeds (see _cache_acted_element),
        # so that each cached template carries meaningful action context
        # (what the element does, not just what it looks like).

        # Generate annotated screenshot with vc_N numbered bounding boxes.
        element_dicts = [
            {"bbox": list(e.bbox), "label": e.label, "confidence": e.confidence}
            for e, _ in all_detected
        ]
        annotated_bytes = vision.annotate_screenshot(screenshot_bytes, element_dicts)
        annotated_b64_raw = base64.b64encode(annotated_bytes).decode("ascii")
        annotated_b64 = _save_screenshot_to_file(
            annotated_b64_raw, "vision", self._session_id,
        )
        annotated_b64 = f"[screenshot saved: {annotated_b64}]"

        # Confidence gating warning.
        interrupted_reason = None
        if max_confidence < _VISION_MIN_CONFIDENCE:
            interrupted_reason = (
                f"Vision detection confidence is very low (max={max_confidence:.3f}). "
                f"Elements may be unreliable. Use force_fallback=True to click."
            )

        logger.info(
            "DesktopBridgeWorker: tier detection returned %d vc_ elements "
            "(max_confidence=%.3f, tiers=%s).",
            len(vision_nodes), max_confidence,
            ", ".join(tr.source_name for tr in tier_results if tr.elements),
        )

        # Persist the scan result as pseudo-UIA so the next launch can
        # preload this structure without re-scanning.
        # Pass the raw screenshot so knowledge_base can generate thumbnails
        # for icon-only elements that have no OCR text.
        if vision_nodes and context.get("app_name"):
            try:
                win_w, win_h = context.get("window_size", (1920, 1080))
                node_dicts = [
                    {
                        "uid": n.uid,
                        "tag": n.tag,
                        "text": n.text,
                        "bbox": list(n.bbox) if n.bbox else None,
                        "interactable": n.interactable,
                        "tier": n.tier or "",
                        "confidence": float(n.attributes.get("confidence", 0.5)),
                        # Include cursor type hint for spatial inference.
                        "cursor_type": n.attributes.get("cursor_type", ""),
                    }
                    for n in vision_nodes if n.bbox
                ]
                kb.save_pseudo_uia_tree(
                    app_name=context["app_name"],
                    nodes=node_dicts,
                    window_size=(win_w, win_h),
                    screenshot_bytes=screenshot_bytes,
                )
            except Exception as exc:
                logger.debug("DesktopBridgeWorker: pseudo-UIA save failed: %s", exc)

        return vision_nodes, annotated_b64, interrupted_reason

    # Labels too generic to be worth caching — common menu/UI text.
    _SKIP_CACHE_LABELS = frozenset({
        "文件", "编辑", "查看", "工具", "帮助", "设置", "窗口",
        "file", "edit", "view", "tools", "help", "settings", "window",
        "ok", "cancel", "yes", "no", "close", "open", "save",
        "确定", "取消", "是", "否", "关闭", "打开", "保存",
    })

    def _cache_vision_crops(
        self,
        screenshot_bytes: bytes,
        elements: list[dict],
        window_title: str,
    ) -> None:
        """Crop actionable element templates and save to muscle memory.

        Only caches elements that are:
        - Labeled with a meaningful name (not generic UI text)
        - From interactable cursor regions (HAND/CUSTOM)
        - Not pure OCR text (which the agent can read directly)
        """
        try:
            import cv2
            import numpy as np

            screenshot_arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
            screenshot = cv2.imdecode(screenshot_arr, cv2.IMREAD_COLOR)
            if screenshot is None:
                return

            cached_count = 0
            for elem in elements:
                label = elem.get("label", "")
                if not label:
                    continue

                # Skip generic/common UI labels.
                if label.lower().strip() in self._SKIP_CACHE_LABELS:
                    continue

                # Skip pure text elements — agent reads OCR text directly.
                if elem.get("tag") == "text" and not elem.get("interactable", False):
                    continue

                # Skip very short labels (single chars, likely noise).
                if len(label.strip()) <= 1:
                    continue

                # Already cached? Don't re-save.
                if self._muscle_memory.has_template(label):
                    continue

                bbox = elem.get("bbox", [0, 0, 0, 0])
                if len(bbox) < 4:
                    continue

                x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
                y_end = min(y + h, screenshot.shape[0])
                x_end = min(x + w, screenshot.shape[1])
                y_start = max(0, y)
                x_start = max(0, x)

                if y_end <= y_start or x_end <= x_start:
                    continue

                crop = screenshot[y_start:y_end, x_start:x_end]
                success, png_bytes = cv2.imencode(".png", crop)
                if success:
                    self._muscle_memory.save(
                        label,
                        png_bytes.tobytes(),
                        app_context=window_title,
                    )
                    cached_count += 1

                # Limit per-cycle caching to avoid I/O storm.
                if cached_count >= 10:
                    break

        except ImportError:
            logger.debug("DesktopBridgeWorker: OpenCV not available, skipping crop cache")
        except Exception as exc:
            logger.debug("DesktopBridgeWorker: crop caching failed: %s", exc)

    def _cache_acted_element(
        self,
        uid: str,
        action_type: str,
        action_value: str,
        screenshot_bytes: Optional[bytes],
        ui_changed: Optional[bool],
    ) -> None:
        """Cache a cropped element template AFTER a successful action.

        Only vision elements (vc_*) with known bboxes are cached.
        The cache entry carries action context so the agent can later see
        "I previously clicked this button and the UI changed."
        """
        if not screenshot_bytes:
            return
        entry = self._vision_element_cache.get(uid)
        if not entry:
            return
        label = entry.get("label", "")
        bbox = entry.get("bbox", [])
        if not label or not bbox or len(bbox) < 4:
            return
        # Skip generic labels.
        if label.lower().strip() in self._SKIP_CACHE_LABELS:
            return

        try:
            import cv2
            import numpy as np

            arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
            screenshot = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if screenshot is None:
                return

            x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            # Add 8px padding so the element has visual context.
            pad = 8
            y0 = max(0, y - pad)
            y1 = min(screenshot.shape[0], y + h + pad)
            x0 = max(0, x - pad)
            x1 = min(screenshot.shape[1], x + w + pad)
            if y1 <= y0 or x1 <= x0:
                return

            crop = screenshot[y0:y1, x0:x1]
            ok, png_bytes = cv2.imencode(".png", crop)
            if ok:
                app_context = self._extractor.get_window_title() if self._extractor else ""
                self._muscle_memory.save(
                    semantic_description=label,
                    cropped_image_bytes=png_bytes.tobytes(),
                    app_context=app_context,
                    action_type=action_type,
                    action_value=action_value[:100],
                    ui_changed=ui_changed,
                )
        except ImportError:
            pass
        except Exception as exc:
            logger.debug("DesktopBridgeWorker: action cache failed: %s", exc)

    @staticmethod
    def _make_thumbnail(
        screenshot_bytes: bytes,
        bbox: list[int],
        max_size: int = 80,
    ) -> Optional[str]:
        """Crop an element region and return a small base64 JPEG thumbnail.

        Args:
            screenshot_bytes: Full screenshot as PNG bytes.
            bbox: Bounding box ``[x, y, width, height]``.
            max_size: Maximum dimension (width or height) of the thumbnail.

        Returns:
            Base64-encoded JPEG string (~2-4KB), or ``None`` on failure.
        """
        try:
            import io
            from PIL import Image

            img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
            x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
            x2 = min(x + w, img.width)
            y2 = min(y + h, img.height)
            x = max(0, x)
            y = max(0, y)

            if x2 <= x or y2 <= y:
                return None

            crop = img.crop((x, y, x2, y2))
            crop.thumbnail((max_size, max_size), Image.LANCZOS)

            buf = io.BytesIO()
            crop.save(buf, format="JPEG", quality=60)
            return base64.b64encode(buf.getvalue()).decode("ascii")

        except Exception:
            return None

    # ------------------------------------------------------------------
    # Screenshot
    # ------------------------------------------------------------------

    async def _handle_screenshot(self, msg: dict) -> None:
        """Capture a screenshot of the desktop and return as base64."""
        req_id = msg.get("data", {}).get("request_id")
        result = await asyncio.get_event_loop().run_in_executor(
            None, self._screenshot_sync
        )
        resp = {
            "type": "screenshot_result",
            "session_id": self._session_id,
            "data": result,
        }
        if req_id:
            resp["request_id"] = req_id
        await self._send_json(resp)

    def _screenshot_sync(self) -> dict:
        """Synchronous window screenshot (runs in thread pool).

        Brings target window to foreground, then captures it.
        """
        try:
            self._ensure_target_focused()
            png_bytes = self._capture_screenshot_bytes()
            if png_bytes is None:
                return {"success": False, "error": "Screenshot capture failed"}

            b64 = base64.b64encode(png_bytes).decode("ascii")
            title = ""
            try:
                import ctypes
                user32 = ctypes.windll.user32
                hwnd = self._target_hwnd or user32.GetForegroundWindow()
                length = user32.GetWindowTextLengthW(hwnd) + 1
                title_buf = ctypes.create_unicode_buffer(length)
                user32.GetWindowTextW(hwnd, title_buf, length)
                title = title_buf.value
            except Exception:
                pass

            return {"success": True, "image_b64": b64, "title": title}
        except Exception as exc:
            logger.error("DesktopBridgeWorker: screenshot failed: %s", exc, exc_info=True)
            return {"success": False, "error": str(exc)}

    # ------------------------------------------------------------------
    # Action execution
    # ------------------------------------------------------------------

    async def _handle_action(self, msg: dict) -> None:
        """Parse and execute an action request."""
        req_id = msg.get("data", {}).get("request_id")
        try:
            action = ActionRequest(**msg.get("data", {}))
        except Exception as exc:
            resp = {
                "type": "action_result",
                "session_id": self._session_id,
                "data": {
                    "success": False,
                    "action_type": msg.get("data", {}).get("action_type", "unknown"),
                    "error": f"Invalid action request: {exc}",
                },
            }
            if req_id:
                resp["request_id"] = req_id
            await self._send_json(resp)
            return

        result = await asyncio.get_event_loop().run_in_executor(
            None, self._act_sync, action
        )
        self._last_action = action

        resp = {
            "type": "action_result",
            "session_id": self._session_id,
            "data": result.model_dump(),
        }
        if req_id:
            resp["request_id"] = req_id
        await self._send_json(resp)

        # If the action result contains an interrupt, also send a ui_interrupt
        # WebSocket message so the daemon can trigger push_interrupt() flow.
        if result.error and "interrupt:" in result.error:
            await self._send_json({
                "type": "ui_interrupt",
                "session_id": self._session_id,
                "data": {
                    "interrupt_type": "modal",
                    "description": result.error.replace("interrupt: ", "", 1),
                },
            })

    def _act_sync(self, action: ActionRequest) -> ActionResult:
        """Execute an action synchronously (runs in thread pool).

        COM must be initialised per-thread for UIA lookups.
        """
        import ctypes
        ctypes.windll.ole32.CoInitialize(0)
        start = time.perf_counter()

        try:
            # Resolve and focus the target window before any interaction.
            # Without this, _target_hwnd may be None after bridge restart
            # or launch_app, causing SendInput to go to the wrong window.
            if action.action_type not in ("wait", "launch_app"):
                if not self._target_hwnd:
                    self._update_target_hwnd()
                focused = self._ensure_target_focused()
                if not focused:
                    # Second attempt: use Alt trick directly in case
                    # AttachThreadInput failed from the thread pool.
                    focused = self._force_foreground()
                if not focused:
                    elapsed = (time.perf_counter() - start) * 1000
                    return ActionResult(
                        success=False,
                        action_type=action.action_type,
                        target_uid=action.target_uid,
                        error=(
                            f"Cannot focus target window (HWND={self._target_hwnd}). "
                            f"SendInput would go to the wrong window. "
                            f"Try perceive first, or launch the app."
                        ),
                        elapsed_ms=elapsed,
                    )
            # Resolve target coordinates for element-specific actions.
            x: Optional[int] = None
            y: Optional[int] = None
            used_vision = False

            if action.target_uid and action.action_type in ("click", "hover", "select"):
                # 4d: Check vc_ vision element cache first.
                if action.target_uid.startswith("vc_"):
                    vc_elem = self._vision_element_cache.get(action.target_uid)
                    if vc_elem:
                        bbox = vc_elem["bbox"]
                        confidence = vc_elem.get("confidence", 0.0)

                        # Low-confidence elements require force_fallback.
                        if confidence < _VISION_MIN_CONFIDENCE and not action.force_fallback:
                            elapsed = (time.perf_counter() - start) * 1000
                            return ActionResult(
                                success=False,
                                action_type=action.action_type,
                                target_uid=action.target_uid,
                                error=(
                                    f"Vision element {action.target_uid} has low "
                                    f"confidence ({confidence:.3f}). Set "
                                    f"force_fallback=True to click anyway."
                                ),
                                elapsed_ms=elapsed,
                            )

                        # Bboxes are already screen-absolute (normalized in _perceive_sync).
                        x = int(bbox[0] + bbox[2] / 2)
                        y = int(bbox[1] + bbox[3] / 2)
                        used_vision = True
                    else:
                        elapsed = (time.perf_counter() - start) * 1000
                        return ActionResult(
                            success=False,
                            action_type=action.action_type,
                            target_uid=action.target_uid,
                            error=(
                                f"Vision element {action.target_uid} not found in "
                                f"cache. Run perceive again to refresh."
                            ),
                            elapsed_ms=elapsed,
                        )
                else:
                    node = self._element_map.get(action.target_uid)
                    # When force_fallback is set with a value (description),
                    # prefer vision-based location over UIA bbox — the UIA
                    # bbox may point to a container rather than the actual
                    # interactive element inside a custom renderer.
                    if action.force_fallback and action.value:
                        coords = self._vision_locate_sync(action.value)
                        if coords:
                            x, y = coords
                            used_vision = True
                    elif node and node.bbox:
                        bx, by, bw, bh = node.bbox
                        x = bx + bw // 2
                        y = by + bh // 2
                    elif not node:
                        # UIA element not found — use vision fallback.
                        coords = self._vision_locate_sync(
                            action.value or action.target_uid
                        )
                        if coords:
                            x, y = coords
                            used_vision = True
                        else:
                            elapsed = (time.perf_counter() - start) * 1000
                            return ActionResult(
                                success=False,
                                action_type=action.action_type,
                                target_uid=action.target_uid,
                                error=(
                                    f"Element {action.target_uid} not found via UIA "
                                    f"or vision fallback."
                                ),
                                elapsed_ms=elapsed,
                            )

            # Also handle force_fallback for type/click without target_uid match.
            if action.force_fallback and x is None and action.value:
                coords = self._vision_locate_sync(action.value)
                if coords:
                    x, y = coords
                    used_vision = True

            # Capture pre-action state for interrupt detection.
            self._desktop_shield.capture_pre_action_state(
                target_hwnd=self._target_hwnd
            )

            # === VISUAL GATEKEEPER (spec section 5.3) ===
            # For oc_ and pk_ nodes, verify the target region has visible content
            # before acting. Skip for vc_ (already visually verified) and
            # non-positional actions (press_key, wait, launch_app).
            if (x is not None and y is not None
                    and action.target_uid
                    and (action.target_uid.startswith("oc_") or action.target_uid.startswith("pk_"))
                    and action.action_type in ("click", "type", "select", "hover")):
                node = self._element_map.get(action.target_uid)
                if node and node.bbox:
                    # Convert screen-absolute bbox to window-relative for gatekeeper
                    # (screenshot is captured relative to window origin).
                    _gk_ox, _gk_oy = self._window_offset
                    gk_bbox = (
                        max(0, node.bbox[0] - _gk_ox),
                        max(0, node.bbox[1] - _gk_oy),
                        node.bbox[2],
                        node.bbox[3],
                    )
                    gk_passed = False
                    for gk_attempt in range(3):
                        gk_screenshot = self._capture_screenshot_bytes()
                        if gk_screenshot and visual_gatekeeper_check(gk_bbox, gk_screenshot):
                            gk_passed = True
                            break
                        logger.info(
                            "DesktopBridgeWorker: visual gatekeeper attempt %d/3 failed for %s",
                            gk_attempt + 1, action.target_uid,
                        )
                        time.sleep(0.3)

                    if not gk_passed:
                        elapsed = (time.perf_counter() - start) * 1000
                        return ActionResult(
                            success=False,
                            action_type=action.action_type,
                            target_uid=action.target_uid,
                            error=(
                                f"Visual gatekeeper: target {action.target_uid} not visible "
                                f"after 3 retries. The element may have moved or the window "
                                f"may not have finished rendering. Try perceive again."
                            ),
                            elapsed_ms=elapsed,
                        )

            # === UIA PATTERN INVOCATION (spec section 5.2.2) ===
            if (action.target_uid
                    and not used_vision
                    and action.target_uid in self._element_map):
                node = self._element_map[action.target_uid]
                if node.attributes.get("prefer_uia") == "true":
                    uia_result = self._try_uia_invoke(
                        action.target_uid, action.action_type, action.value,
                    )
                    if uia_result:
                        return uia_result
                    logger.info(
                        "DesktopBridgeWorker: UIA pattern unavailable for %s, falling back to coords",
                        action.target_uid,
                    )

            # Danger zone safety check — block actions on payment/UAC/deletion dialogs.
            if action.action_type not in ("wait", "launch_app"):
                _dz_title = self._get_current_window_title()
                _dz_warning = self._desktop_shield.check_danger_zone(
                    _dz_title, self._target_class_name
                )
                if _dz_warning:
                    elapsed = (time.perf_counter() - start) * 1000
                    return ActionResult(
                        success=False,
                        action_type=action.action_type,
                        target_uid=action.target_uid,
                        error=f"SAFETY: {_dz_warning}",
                        elapsed_ms=elapsed,
                    )

            # Capture pre-action screenshot for visual diff verification.
            _pre_shot = self._capture_screenshot_bytes()
            if _pre_shot:
                self._desktop_shield.capture_pre_action_screenshot(_pre_shot)

            # Pre-action focus re-verification: ensure target window is
            # still in the foreground right before dispatching the click.
            # Focus can be stolen between perceive and act.
            if action.action_type in ("click", "type", "select") and self._target_hwnd:
                try:
                    import ctypes as _ct
                    fg = _ct.windll.user32.GetForegroundWindow()
                    if fg != self._target_hwnd:
                        logger.info(
                            "DesktopBridgeWorker: focus lost before action "
                            "(fg=%s, target=%s), re-focusing.",
                            fg, self._target_hwnd,
                        )
                        self._ensure_target_focused()
                        time.sleep(0.05)
                except Exception as _focus_exc:
                    logger.debug("DesktopBridgeWorker: pre-action focus check: %s", _focus_exc)

            # Dispatch by action type.
            if action.action_type == "click":
                if x is None or y is None:
                    elapsed = (time.perf_counter() - start) * 1000
                    return ActionResult(
                        success=False,
                        action_type=action.action_type,
                        target_uid=action.target_uid,
                        error=(
                            f"Cannot click: element coordinates not resolved. "
                            f"UIA and vision fallback both failed to locate "
                            f"'{action.target_uid or action.value}'. "
                            f"Try perceive first, or use --force-fallback with "
                            f"a descriptive --value."
                        ),
                        elapsed_ms=elapsed,
                    )
                phys.click(x, y)
                method = "vision" if used_vision else "UIA"
                msg = f"Clicked at ({x}, {y}) via {method} for {action.target_uid}."

            elif action.action_type == "type":
                # If there's a target, click it first to focus.
                if x is not None and y is not None:
                    phys.click(x, y)
                    time.sleep(0.1)
                phys.type_text(action.value or "")
                msg = f"Typed {len(action.value or '')} chars."

            elif action.action_type == "press_key":
                key = action.value or "enter"
                # Handle combo keys like "ctrl+c".
                if "+" in key:
                    parts = [k.strip() for k in key.split("+")]
                    phys.press_combo(*parts)
                else:
                    phys.press_key(key)
                msg = f"Pressed key: {action.value}."

            elif action.action_type == "scroll":
                # Desktop scroll via mouse wheel -- not directly in
                # physical_input, so we use keyboard Page Up/Down as proxy.
                direction = (action.value or "down").lower()
                if direction == "up":
                    phys.press_key("pageup")
                else:
                    phys.press_key("pagedown")
                msg = f"Scrolled {direction}."

            elif action.action_type == "hover":
                if x is not None and y is not None:
                    phys.move_to(x, y)
                msg = f"Hovered at ({x}, {y})."

            elif action.action_type == "select":
                # For desktop, select = click + type the option value.
                if x is not None and y is not None:
                    phys.click(x, y)
                    time.sleep(0.2)
                if action.value:
                    phys.type_text(action.value)
                    phys.press_key("enter")
                msg = f"Selected '{action.value}' on {action.target_uid}."

            elif action.action_type == "wait":
                try:
                    duration = float(action.value) if action.value else 1.0
                except ValueError:
                    duration = 1.0
                duration = max(0.1, min(duration, 30.0))
                time.sleep(duration)
                msg = f"Waited {duration:.1f}s."

            elif action.action_type == "launch_app":
                app_name = action.value or ""
                if not app_name:
                    elapsed = (time.perf_counter() - start) * 1000
                    return ActionResult(
                        success=False,
                        action_type="launch_app",
                        error="No app name provided in 'value'.",
                        elapsed_ms=elapsed,
                    )
                # Resolve the app name to a full path.
                resolved = app_launcher.resolve_app(app_name)
                if resolved:
                    import subprocess
                    try:
                        subprocess.Popen(
                            [resolved],
                            creationflags=0x00000008,  # DETACHED_PROCESS
                        )
                        msg = f"Launched '{app_name}' from '{resolved}'."
                        # Wait for the app to create its window, then find
                        # it by title matching instead of using foreground.
                        self._target_hwnd = None
                        self._target_class_name = None
                        self._target_title_pattern = None
                        time.sleep(2.0)
                        self._find_and_target_by_name(app_name)
                        if self._target_hwnd:
                            self._ensure_target_focused()
                    except Exception as launch_exc:
                        elapsed = (time.perf_counter() - start) * 1000
                        return ActionResult(
                            success=False,
                            action_type="launch_app",
                            error=f"Failed to launch '{resolved}': {launch_exc}",
                            elapsed_ms=elapsed,
                        )
                else:
                    # Fallback: try os.startfile which handles Start Menu apps.
                    try:
                        os.startfile(app_name)
                        msg = f"Launched '{app_name}' via os.startfile."
                        self._target_hwnd = None
                        self._target_class_name = None
                        self._target_title_pattern = None
                    except Exception as sf_exc:
                        elapsed = (time.perf_counter() - start) * 1000
                        return ActionResult(
                            success=False,
                            action_type="launch_app",
                            error=(
                                f"Could not resolve '{app_name}' to an executable. "
                                f"os.startfile also failed: {sf_exc}. "
                                f"Try providing the full path."
                            ),
                            elapsed_ms=elapsed,
                        )

            else:
                elapsed = (time.perf_counter() - start) * 1000
                return ActionResult(
                    success=False,
                    action_type=action.action_type,
                    target_uid=action.target_uid,
                    error=f"Unknown action type: {action.action_type}",
                    elapsed_ms=elapsed,
                )

            elapsed = (time.perf_counter() - start) * 1000

            # Post-action visual diff for all interactive actions.
            verification_b64 = None
            ui_changed: Optional[bool] = None
            post_shot: Optional[bytes] = None
            if action.action_type in ("click", "type", "select", "press_key", "scroll"):
                time.sleep(_VERIFY_DELAY_S)
                post_shot = self._capture_screenshot_bytes()
                if post_shot:
                    raw_b64 = base64.b64encode(post_shot).decode("ascii")
                    path = _save_screenshot_to_file(raw_b64, "post_action", self._session_id)
                    verification_b64 = f"[screenshot saved: {path}]"
                    diff_ratio = self._desktop_shield.compute_visual_diff(post_shot)
                    if diff_ratio is not None:
                        ui_changed = diff_ratio > 0.005  # 0.5% pixel change threshold
                        if not ui_changed and action.action_type in ("click", "type"):
                            logger.warning(
                                "DesktopBridgeWorker: no visual change after %s "
                                "on %s (diff_ratio=%.5f). Click may not have "
                                "reached the target.",
                                action.action_type, action.target_uid, diff_ratio,
                            )
                            msg = (
                                f"{msg} [Warning: 未检测到UI变化 (diff={diff_ratio:.5f})，"
                                f"操作可能未生效。建议重新 perceive 确认状态。]"
                            )

            # T2 action-contextual caching: cache ONLY on successful action.
            # This attaches meaning to the template — "I clicked this and X happened".
            if action.action_type in ("click", "type") and action.target_uid:
                self._cache_acted_element(
                    uid=action.target_uid,
                    action_type=action.action_type,
                    action_value=action.value or "",
                    screenshot_bytes=_pre_shot,
                    ui_changed=ui_changed,
                )

            elif used_vision:
                verification_b64 = self._capture_verification_screenshot()

            # Post-action interrupt detection.
            interrupt = self._desktop_shield.detect_post_action_changes()
            if interrupt:
                logger.info(
                    "DesktopBridgeWorker: interrupt detected after action: %s",
                    interrupt.get("description", ""),
                )
                return ActionResult(
                    success=True,
                    action_type=action.action_type,
                    target_uid=action.target_uid,
                    message=msg,
                    elapsed_ms=elapsed,
                    verification_screenshot=verification_b64,
                    ui_change_detected=ui_changed,
                    error=(
                        f"interrupt: {interrupt['description']}"
                    ),
                )

            return ActionResult(
                success=True,
                action_type=action.action_type,
                target_uid=action.target_uid,
                message=msg,
                elapsed_ms=elapsed,
                verification_screenshot=verification_b64,
                ui_change_detected=ui_changed,
            )

        except NotImplementedError as exc:
            elapsed = (time.perf_counter() - start) * 1000
            return ActionResult(
                success=False,
                action_type=action.action_type,
                target_uid=action.target_uid,
                error=str(exc),
                elapsed_ms=elapsed,
            )
        except Exception as exc:
            elapsed = (time.perf_counter() - start) * 1000
            logger.error("DesktopBridgeWorker: action error: %s", exc, exc_info=True)
            return ActionResult(
                success=False,
                action_type=action.action_type,
                target_uid=action.target_uid,
                error=f"Unexpected error: {exc}",
                elapsed_ms=elapsed,
            )

    def _vision_locate_sync(self, description: str) -> Optional[tuple[int, int]]:
        """Take a screenshot and use vision fallback to find an element.

        Uses the synchronous T2 (fast_match) path first. T3 (VLM) requires
        async, so we run it in a nested event loop if T2 misses.
        """
        try:
            screenshot_bytes = self._capture_screenshot_bytes()
            if screenshot_bytes is None:
                logger.warning("DesktopBridgeWorker: screenshot capture failed for vision fallback")
                return None

            # Try T2 first (synchronous, fast).
            coords = vision.fast_match(screenshot_bytes, description)
            if coords is not None:
                logger.info("DesktopBridgeWorker: T2 vision match at (%d, %d) for %r", coords[0], coords[1], description)
                return coords

            # T3 requires async — run in a new event loop.
            import asyncio
            try:
                loop = asyncio.new_event_loop()
                coords = loop.run_until_complete(vision.find_target(screenshot_bytes, description))
                loop.close()
            except Exception as exc:
                logger.warning("DesktopBridgeWorker: T3 vision fallback error: %s", exc)
                return None

            if coords:
                logger.info("DesktopBridgeWorker: T3 vision match at (%d, %d) for %r", coords[0], coords[1], description)
            return coords
        except Exception as exc:
            logger.error("DesktopBridgeWorker: vision locate error: %s", exc, exc_info=True)
            return None

    # ------------------------------------------------------------------
    # Target HWND resolution
    # ------------------------------------------------------------------

    def _resolve_target_hwnd(self) -> Optional[int]:
        """Find the best window HWND to capture.

        Priority:
        1. Stored ``_target_hwnd`` if it's still a valid, visible window.
        2. Re-find by stored class name / title pattern.
        3. Foreground window, if it's NOT a terminal.
        4. Largest visible non-terminal top-level window.
        5. None (caller should fall back to fullscreen).
        """
        import ctypes

        user32 = ctypes.windll.user32

        # 1. Check stored target HWND.
        if self._target_hwnd:
            if user32.IsWindow(self._target_hwnd) and user32.IsWindowVisible(self._target_hwnd):
                logger.debug(
                    "DesktopBridgeWorker: using stored target HWND %s",
                    self._target_hwnd,
                )
                return self._target_hwnd
            else:
                logger.debug("DesktopBridgeWorker: stored target HWND no longer valid")
                self._target_hwnd = None

        # 2. Re-find by identity.
        if self._target_class_name or self._target_title_pattern:
            hwnd = self._find_window_by_identity()
            if hwnd:
                self._target_hwnd = hwnd
                logger.debug(
                    "DesktopBridgeWorker: re-found target by identity (HWND=%s)",
                    hwnd,
                )
                return hwnd

        # 3. Check foreground window.
        fg_hwnd = user32.GetForegroundWindow()
        if fg_hwnd and not self._is_terminal_window(fg_hwnd):
            return fg_hwnd

        # 4. Largest non-terminal window.
        best_hwnd = self._find_largest_non_terminal()
        if best_hwnd:
            logger.debug(
                "DesktopBridgeWorker: resolved target via enumeration (HWND=%s)",
                best_hwnd,
            )
        return best_hwnd

    @staticmethod
    def _is_terminal_window(hwnd) -> bool:
        """Check if a window HWND belongs to a terminal / console application."""
        try:
            import ctypes

            user32 = ctypes.windll.user32

            # Check window class name.
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            class_name = class_buf.value
            if class_name in _TERMINAL_CLASS_NAMES:
                return True

            # Check window title for terminal-like patterns.
            length = user32.GetWindowTextLengthW(hwnd) + 1
            title_buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, title_buf, length)
            title_lower = title_buf.value.lower()

            for substr in _TERMINAL_EXE_SUBSTRINGS:
                if substr in title_lower:
                    return True

            return False
        except Exception:
            return False

    def _update_target_hwnd(self) -> None:
        """Find and store the target application window HWND.

        Strategy (never relies on GetForegroundWindow alone):
        1. Re-find by stored class name / title pattern (most reliable).
        2. Use UIA extractor's last HWND if valid and non-terminal.
        3. Foreground window ONLY if it's not a terminal.
        4. Largest non-terminal visible window as last resort.

        Also records the window's class name and title for future lookups.
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            # 1. Re-find by stored identity (class + title pattern).
            if self._target_class_name or self._target_title_pattern:
                hwnd = self._find_window_by_identity()
                if hwnd:
                    self._target_hwnd = hwnd
                    logger.debug(
                        "DesktopBridgeWorker: target found by identity (HWND=%s)",
                        hwnd,
                    )
                    return

            # 2. Check stored HWND validity.
            if self._target_hwnd:
                if user32.IsWindow(self._target_hwnd) and user32.IsWindowVisible(self._target_hwnd):
                    if not self._is_terminal_window(self._target_hwnd):
                        return  # Still valid
                self._target_hwnd = None

            # 3. Foreground window if not a terminal.
            fg_hwnd = user32.GetForegroundWindow()
            if fg_hwnd and not self._is_terminal_window(fg_hwnd):
                self._target_hwnd = fg_hwnd
                self._record_window_identity(fg_hwnd)
                logger.info(
                    "DesktopBridgeWorker: target HWND set from foreground: %s",
                    fg_hwnd,
                )
                return

            # 4. Largest non-terminal visible window.
            hwnd = self._find_largest_non_terminal()
            if hwnd:
                self._target_hwnd = hwnd
                self._record_window_identity(hwnd)
                logger.info(
                    "DesktopBridgeWorker: target HWND set from enumeration: %s",
                    hwnd,
                )

        except Exception as exc:
            logger.debug("DesktopBridgeWorker: failed to update target HWND: %s", exc)

    def _force_foreground(self) -> bool:
        """Aggressively bring _target_hwnd to the foreground.

        Uses multiple strategies that work from background threads:
        1. Minimize + restore (forces Windows to refocus).
        2. Alt-key trick + SetForegroundWindow.
        3. SetWindowPos with TOPMOST flag (then remove it).
        """
        if not self._target_hwnd:
            return False
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = self._target_hwnd

            if not user32.IsWindow(hwnd):
                return False

            # Strategy 1: Minimize then restore to force focus change.
            SW_MINIMIZE = 6
            SW_RESTORE = 9
            user32.ShowWindow(hwnd, SW_MINIMIZE)
            import time
            time.sleep(0.1)
            user32.ShowWindow(hwnd, SW_RESTORE)
            time.sleep(0.2)

            if user32.GetForegroundWindow() == hwnd:
                return True

            # Strategy 2: Alt key trick.
            VK_MENU = 0x12
            KEYEVENTF_EXTENDEDKEY = 0x0001
            KEYEVENTF_KEYUP = 0x0002
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY, 0)
            user32.SetForegroundWindow(hwnd)
            user32.keybd_event(VK_MENU, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
            time.sleep(0.2)

            if user32.GetForegroundWindow() == hwnd:
                return True

            # Strategy 3: Temporarily set TOPMOST.
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOMOVE = 0x0002
            SWP_NOSIZE = 0x0001
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(
                hwnd, HWND_TOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
            user32.SetWindowPos(
                hwnd, HWND_NOTOPMOST, 0, 0, 0, 0,
                SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW,
            )
            user32.SetForegroundWindow(hwnd)
            time.sleep(0.15)
            return user32.GetForegroundWindow() == hwnd
        except Exception as exc:
            logger.debug("DesktopBridgeWorker: _force_foreground failed: %s", exc)
            return False

    def _extract_uia_with_com_init(self) -> list[UIDNode]:
        """UIA extraction with per-thread COM initialization."""
        import ctypes
        ctypes.windll.ole32.CoInitialize(None)
        try:
            return self._extractor.extract()
        finally:
            ctypes.windll.ole32.CoUninitialize()

    def _try_uia_invoke(
        self, target_uid: str, action_type: str, value: Optional[str],
    ) -> Optional[ActionResult]:
        """Attempt native UIA pattern invocation for prefer_uia nodes.

        Re-finds the UIA element at act time via automation-id or bbox,
        then calls InvokePattern.Invoke() or ValuePattern.SetValue().

        Returns ActionResult on success, None if UIA invocation unavailable
        (caller should fall back to coordinate click).
        """
        if not _PREFER_UIA_ENABLED or not _IS_WINDOWS or auto is None:
            return None

        node = self._element_map.get(target_uid)
        if not node:
            return None

        try:
            foreground = auto.GetForegroundControl()
            if not foreground:
                return None

            # Strategy 1: Find by automation-id.
            target_control = None
            aid = node.attributes.get("automation-id", "")
            if aid:
                target_control = foreground.Control(AutomationId=aid)

            # Strategy 2: Find by bbox proximity + tag match.
            if not target_control and node.bbox:
                bx, by, bw, bh = node.bbox
                cx, cy = bx + bw // 2, by + bh // 2
                best = None
                best_dist = 15  # Max tolerance
                for child in foreground.GetChildren():
                    try:
                        rect = child.BoundingRectangle
                        ccx = (rect.left + rect.right) // 2
                        ccy = (rect.top + rect.bottom) // 2
                        dist = abs(cx - ccx) + abs(cy - ccy)
                        if dist < best_dist:
                            best_dist = dist
                            best = child
                    except Exception:
                        continue
                target_control = best

            if not target_control:
                return None

            # Invoke the appropriate pattern.
            if action_type == "click":
                invoke = target_control.GetInvokePattern()
                if invoke:
                    invoke.Invoke()
                    return ActionResult(
                        success=True,
                        action_type=action_type,
                        target_uid=target_uid,
                        message="Invoked via UIA InvokePattern (native)",
                    )
            elif action_type == "type" and value:
                vp = target_control.GetValuePattern()
                if vp:
                    vp.SetValue(value)
                    return ActionResult(
                        success=True,
                        action_type=action_type,
                        target_uid=target_uid,
                        message=f"Set value via UIA ValuePattern: '{value[:50]}'",
                    )

        except Exception as exc:
            logger.debug("DesktopBridgeWorker: UIA pattern invocation failed: %s", exc)

        return None

    def _run_vision_waterfall_thread(
        self, screenshot_bytes: bytes, region: Optional[list[int]],
    ) -> Optional[tuple[list[UIDNode], str, Optional[str]]]:
        """Vision waterfall with per-thread COM initialization."""
        import ctypes
        ctypes.windll.ole32.CoInitialize(None)
        try:
            return self._vision_perceive_sync(region, screenshot_bytes=screenshot_bytes)
        finally:
            ctypes.windll.ole32.CoUninitialize()

    def _find_and_target_by_name(self, app_name: str) -> None:
        """Find a visible window whose title contains *app_name* (case-insensitive)
        and set it as the target.  Used after launch_app to avoid picking the
        wrong foreground window."""
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            name_lower = app_name.lower()

            # Common app-name-to-title mappings.
            _TITLE_ALIASES: dict[str, list[str]] = {
                "weixin": ["微信"],
                "wechat": ["微信"],
            }
            search_terms = [name_lower] + [
                a for a in _TITLE_ALIASES.get(name_lower, [])
            ]

            candidates: list[tuple[int, str, int]] = []  # (hwnd, title, area)

            @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
            def _enum_cb(hwnd, _lp):
                if not user32.IsWindowVisible(hwnd):
                    return True
                length = user32.GetWindowTextLengthW(hwnd)
                if length <= 0:
                    return True
                buf = ctypes.create_unicode_buffer(length + 1)
                user32.GetWindowTextW(hwnd, buf, length + 1)
                title = buf.value
                title_lower = title.lower()
                for term in search_terms:
                    if term in title_lower:
                        rect = wintypes.RECT()
                        user32.GetWindowRect(hwnd, ctypes.byref(rect))
                        w = rect.right - rect.left
                        h = rect.bottom - rect.top
                        candidates.append((hwnd, title, w * h))
                        break
                return True

            user32.EnumWindows(_enum_cb, 0)

            if candidates:
                # Pick the largest matching window.
                candidates.sort(key=lambda c: c[2], reverse=True)
                best_hwnd, best_title, _ = candidates[0]
                self._target_hwnd = best_hwnd
                self._record_window_identity(best_hwnd)
                logger.info(
                    "DesktopBridgeWorker: target found by app name '%s': "
                    "HWND=%s title='%s'",
                    app_name, best_hwnd, best_title,
                )
            else:
                logger.warning(
                    "DesktopBridgeWorker: no window found matching '%s', "
                    "falling back to _update_target_hwnd",
                    app_name,
                )
                self._update_target_hwnd()
        except Exception as exc:
            logger.warning(
                "DesktopBridgeWorker: _find_and_target_by_name failed: %s", exc
            )
            self._update_target_hwnd()

    def _find_window_by_identity(self) -> Optional[int]:
        """Find a window matching the stored class name and/or title pattern."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        candidates: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_cb(hwnd, _lp):
            if user32.IsWindowVisible(hwnd):
                candidates.append(hwnd)
            return True

        user32.EnumWindows(_enum_cb, 0)

        best_hwnd = None
        best_area = 0

        for hwnd in candidates:
            if self._is_terminal_window(hwnd):
                continue

            # Check class name match.
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            class_name = class_buf.value

            # Check title.
            length = user32.GetWindowTextLengthW(hwnd) + 1
            title_buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, title_buf, length)
            title = title_buf.value

            matches = False
            if self._target_class_name and class_name == self._target_class_name:
                matches = True
            if self._target_title_pattern and self._target_title_pattern in title:
                matches = True

            if not matches:
                continue

            # Pick largest matching window.
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 50 or h < 50:
                continue
            area = w * h
            if area > best_area:
                best_area = area
                best_hwnd = hwnd

        return best_hwnd

    def _find_largest_non_terminal(self) -> Optional[int]:
        """Enumerate visible windows and return the largest non-terminal one."""
        import ctypes
        from ctypes import wintypes

        user32 = ctypes.windll.user32
        candidates: list[int] = []

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def _enum_cb(hwnd, _lp):
            if user32.IsWindowVisible(hwnd):
                candidates.append(hwnd)
            return True

        user32.EnumWindows(_enum_cb, 0)

        best_hwnd = None
        best_area = 0
        for hwnd in candidates:
            if self._is_terminal_window(hwnd):
                continue
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            w = rect.right - rect.left
            h = rect.bottom - rect.top
            if w < 200 or h < 150:
                continue
            area = w * h
            if area > best_area:
                best_area = area
                best_hwnd = hwnd

        return best_hwnd

    def _record_window_identity(self, hwnd) -> None:
        """Store the class name and title of a window for future lookups."""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            class_buf = ctypes.create_unicode_buffer(256)
            user32.GetClassNameW(hwnd, class_buf, 256)
            self._target_class_name = class_buf.value or None

            length = user32.GetWindowTextLengthW(hwnd) + 1
            title_buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, title_buf, length)
            title = title_buf.value
            # Store the main portion of the title as pattern.
            # Avoid storing dynamic suffixes (e.g. message counts).
            if title:
                self._target_title_pattern = title.split(" - ")[0].split(" — ")[0].strip()[:50] or None
            else:
                self._target_title_pattern = None

            logger.debug(
                "DesktopBridgeWorker: recorded identity class=%r title_pattern=%r",
                self._target_class_name, self._target_title_pattern,
            )
        except Exception as exc:
            logger.debug("DesktopBridgeWorker: failed to record identity: %s", exc)

    def _ensure_target_focused(self) -> bool:
        """Bring the target application window to the foreground.

        Uses a combination of SetWindowPos(TOPMOST) and AttachThreadInput to
        force the target window to the front.  This is critical for reliable
        physical input on Windows, as background windows often ignore events.

        Returns:
            True if the target window is now foreground, False otherwise.
        """
        if not self._target_hwnd:
            return False
        try:
            import ctypes
            import ctypes.wintypes as wintypes
            user32 = ctypes.windll.user32
            kernel32 = ctypes.windll.kernel32

            hwnd = self._target_hwnd
            if not user32.IsWindow(hwnd):
                return False

            # Restore if minimized, and ensure it's shown.
            SW_SHOW = 5
            SW_RESTORE = 9
            if user32.IsIconic(hwnd):
                user32.ShowWindow(hwnd, SW_RESTORE)
            else:
                user32.ShowWindow(hwnd, SW_SHOW)

            # --- Force Focus Trick ---
            # 1. Bring to top via SetWindowPos (briefly set as TOPMOST then back)
            HWND_TOPMOST = -1
            HWND_NOTOPMOST = -2
            SWP_NOSIZE = 0x0001
            SWP_NOMOVE = 0x0002
            SWP_SHOWWINDOW = 0x0040
            user32.SetWindowPos(hwnd, HWND_TOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)
            user32.SetWindowPos(hwnd, HWND_NOTOPMOST, 0, 0, 0, 0, SWP_NOSIZE | SWP_NOMOVE | SWP_SHOWWINDOW)

            # 2. AttachThreadInput trick to bypass foreground lock.
            fg_hwnd = user32.GetForegroundWindow()
            fg_tid = user32.GetWindowThreadProcessId(fg_hwnd, None)
            our_tid = kernel32.GetCurrentThreadId()

            attached = False
            if fg_tid != our_tid and fg_tid != 0:
                attached = bool(user32.AttachThreadInput(our_tid, fg_tid, True))

            try:
                user32.BringWindowToTop(hwnd)
                user32.SetForegroundWindow(hwnd)
            finally:
                if attached:
                    user32.AttachThreadInput(our_tid, fg_tid, False)

            # Wait a moment for Windows to catch up.
            time.sleep(0.25)
            return user32.GetForegroundWindow() == hwnd
        except Exception as exc:
            logger.debug("DesktopBridgeWorker: _ensure_target_focused failed: %s", exc)
            return False

    # ------------------------------------------------------------------
    # Screenshot capture
    # ------------------------------------------------------------------

    def _get_current_window_title(self) -> str:
        """Return the title of the current target window (or foreground window)."""
        try:
            import ctypes
            user32 = ctypes.windll.user32
            hwnd = self._target_hwnd or user32.GetForegroundWindow()
            length = user32.GetWindowTextLengthW(hwnd) + 1
            buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, buf, length)
            return buf.value
        except Exception:
            return ""

    def _capture_screenshot_bytes(self) -> Optional[bytes]:
        """Capture the target application window as PNG bytes.

        Window resolution order:
        1. Stored ``_target_hwnd`` (set during last perceive) — if still valid.
        2. ``GetForegroundWindow()`` — only if it's NOT a terminal window.
        3. Enumerate top-level windows and pick the largest non-terminal one.
        4. Full-screen fallback.

        Side-effect: updates ``self._window_offset`` with the window's
        screen-space ``(left, top)`` so that vision bboxes can be
        translated to absolute screen coordinates for clicking.
        """
        try:
            import ctypes
            import io
            from ctypes import wintypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            hwnd = self._resolve_target_hwnd()
            if not hwnd:
                logger.debug("DesktopBridgeWorker: no suitable window found, falling back to fullscreen")
                return self._capture_fullscreen_bytes()

            # Get window rect.
            rect = wintypes.RECT()
            if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                logger.debug("DesktopBridgeWorker: GetWindowRect failed, falling back to fullscreen")
                return self._capture_fullscreen_bytes()

            width = rect.right - rect.left
            height = rect.bottom - rect.top
            if width < 10 or height < 10:
                logger.debug("DesktopBridgeWorker: window too small (%dx%d), falling back to fullscreen", width, height)
                return self._capture_fullscreen_bytes()

            self._window_offset = (rect.left, rect.top)

            # Try PrintWindow (PW_RENDERFULLCONTENT = 2) first.
            hdc_screen = user32.GetDC(0)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
            gdi32.SelectObject(hdc_mem, hbmp)
            user32.ReleaseDC(0, hdc_screen)

            PW_RENDERFULLCONTENT = 2
            printed = user32.PrintWindow(hwnd, hdc_mem, PW_RENDERFULLCONTENT)

            if not printed:
                # Fallback: BitBlt from window DC.
                hdc_win = user32.GetWindowDC(hwnd)
                gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_win, 0, 0, 0x00CC0020)
                user32.ReleaseDC(hwnd, hdc_win)

            png_bytes = self._hbmp_to_png(hdc_mem, hbmp, width, height)

            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)

            if png_bytes is None:
                return self._capture_fullscreen_bytes()

            # Check for black image (window not rendered properly).
            try:
                from PIL import Image
                img = Image.open(io.BytesIO(png_bytes))
                extrema = img.convert("L").getextrema()
                if extrema[1] < 5:  # max pixel value < 5 → essentially black
                    logger.debug("DesktopBridgeWorker: window capture returned black image, falling back to fullscreen")
                    self._window_offset = (0, 0)
                    return self._capture_fullscreen_bytes()
            except Exception:
                pass  # If we can't verify, trust the capture.

            return png_bytes

        except Exception as exc:
            logger.error("DesktopBridgeWorker: window screenshot error: %s", exc)
            self._window_offset = (0, 0)
            return self._capture_fullscreen_bytes()

    def _capture_fullscreen_bytes(self) -> Optional[bytes]:
        """Capture the entire screen as PNG bytes (fallback)."""
        try:
            import ctypes

            user32 = ctypes.windll.user32
            gdi32 = ctypes.windll.gdi32

            width = user32.GetSystemMetrics(0)
            height = user32.GetSystemMetrics(1)
            self._window_offset = (0, 0)

            hdc_screen = user32.GetDC(0)
            hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
            hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, width, height)
            gdi32.SelectObject(hdc_mem, hbmp)
            gdi32.BitBlt(hdc_mem, 0, 0, width, height, hdc_screen, 0, 0, 0x00CC0020)
            user32.ReleaseDC(0, hdc_screen)

            png_bytes = self._hbmp_to_png(hdc_mem, hbmp, width, height)

            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)

            return png_bytes

        except Exception as exc:
            logger.error("DesktopBridgeWorker: fullscreen screenshot error: %s", exc)
            return None

    @staticmethod
    def _hbmp_to_png(hdc_mem, hbmp, width: int, height: int) -> Optional[bytes]:
        """Convert an in-memory HBITMAP to PNG bytes via PIL."""
        try:
            import ctypes
            import io

            gdi32 = ctypes.windll.gdi32

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.c_uint32), ("biWidth", ctypes.c_int32),
                    ("biHeight", ctypes.c_int32), ("biPlanes", ctypes.c_uint16),
                    ("biBitCount", ctypes.c_uint16), ("biCompression", ctypes.c_uint32),
                    ("biSizeImage", ctypes.c_uint32), ("biXPelsPerMeter", ctypes.c_int32),
                    ("biYPelsPerMeter", ctypes.c_int32), ("biClrUsed", ctypes.c_uint32),
                    ("biClrImportant", ctypes.c_uint32),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = width
            bmi.biHeight = -height  # Top-down.
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biSizeImage = width * height * 4

            buf = ctypes.create_string_buffer(bmi.biSizeImage)
            gdi32.GetDIBits(hdc_mem, hbmp, 0, height, buf, ctypes.byref(bmi), 0)

            try:
                from PIL import Image
                img = Image.frombytes("RGBA", (width, height), buf.raw, "raw", "BGRA")
                png_io = io.BytesIO()
                img.save(png_io, format="PNG")
                return png_io.getvalue()
            except ImportError:
                logger.warning("DesktopBridgeWorker: PIL not available, vision fallback limited")
                return None

        except Exception as exc:
            logger.error("DesktopBridgeWorker: bitmap to PNG conversion error: %s", exc)
            return None

    def _capture_verification_screenshot(self) -> Optional[str]:
        """Wait briefly, capture screenshot, save to file and return path.

        Returns a ``[screenshot saved: <path>]`` string instead of raw base64
        to avoid bloating the agent's context window.
        """
        try:
            time.sleep(_VERIFY_DELAY_S)
            screenshot_bytes = self._capture_screenshot_bytes()
            if screenshot_bytes is None:
                return None
            b64_data = base64.b64encode(screenshot_bytes).decode("ascii")
            path = _save_screenshot_to_file(b64_data, "verify", self._session_id)
            return f"[screenshot saved: {path}]"
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: verification screenshot failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Rollback
    # ------------------------------------------------------------------

    async def _handle_rollback(self, msg: dict) -> None:
        """Best-effort undo -- currently limited to Ctrl+Z."""
        success = False
        try:
            if self._last_action and self._last_action.action_type in ("type", "click"):
                await asyncio.get_event_loop().run_in_executor(
                    None, phys.press_combo, "ctrl", "z"
                )
                success = True
                self._last_action = None
            else:
                success = True  # Nothing to undo.
        except Exception as exc:
            logger.warning("DesktopBridgeWorker: rollback failed: %s", exc)

        await self._send_json({
            "type": "rollback_result",
            "session_id": self._session_id,
            "data": {"success": success},
        })

    # ------------------------------------------------------------------
    # Utilities
    # ------------------------------------------------------------------

    async def _send_json(self, data: dict) -> None:
        if self._ws:
            try:
                # Recursively ensure all strings are valid UTF-8.
                # This fixes Mojibake where UTF-8 bytes were interpreted as GBK
                # before being wrapped in Python unicode strings.
                def _ensure_utf8(obj):
                    if isinstance(obj, dict):
                        return {k: _ensure_utf8(v) for k, v in obj.items()}
                    if isinstance(obj, list):
                        return [_ensure_utf8(item) for item in obj]
                    if isinstance(obj, str):
                        try:
                            # Heuristic: if it looks like Mojibake (high-entropy non-ASCII),
                            # try to round-trip it through GBK/UTF-8.
                            if any(ord(c) > 0x7F for c in obj):
                                # Common pattern: UTF-8 encoded, then decoded as CP936 (GBK)
                                return obj.encode('cp936').decode('utf-8')
                        except (UnicodeEncodeError, UnicodeDecodeError):
                            pass
                        return obj
                    return obj

                payload = json.dumps(_ensure_utf8(data), default=str, ensure_ascii=False)
                await self._ws.send(payload)
            except Exception as exc:
                logger.warning("DesktopBridgeWorker: failed to send message: %s", exc)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

async def main() -> None:
    """Run the desktop bridge worker as a standalone process."""
    import argparse

    parser = argparse.ArgumentParser(description="OpenClaw Desktop Bridge Worker")
    parser.add_argument("--session-id", default="desktop-default", help="Session identifier")
    parser.add_argument("--daemon-url", default=_DEFAULT_DAEMON_URL, help="Daemon WebSocket URL")
    args = parser.parse_args()

    # Force UTF-8 on all log handlers to avoid GBK encoding errors on
    # Chinese Windows when logging Chinese text, emoji, or special Unicode.
    import io, sys
    if hasattr(sys.stderr, "buffer"):
        sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
    )

    worker = DesktopBridgeWorker(session_id=args.session_id)

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, lambda: asyncio.create_task(worker.stop()))
        except NotImplementedError:
            pass

    await worker.start(daemon_url=args.daemon_url)


if __name__ == "__main__":
    asyncio.run(main())
