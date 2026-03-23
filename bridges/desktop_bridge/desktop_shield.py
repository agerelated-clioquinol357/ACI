"""
OpenClaw 2.0 ACI Framework - Desktop Shield.

Monitors the Windows desktop for UI interrupts that may block the planned
action flow.  Mirrors the :class:`MutationShield` interface used by the
web bridge, but uses Win32 APIs instead of Playwright/DOM observation.

Detects:

* Foreground window HWND change (new window / popup appeared).
* Window title change (state transition within the same window).
* (Optional) Large visual diff (overlay appeared within the window).

Filters out transient tooltips and animations by requiring a minimum
window size and persistence delay.
"""

from __future__ import annotations

import logging
import time
from typing import Optional

logger = logging.getLogger(__name__)

# Minimum window dimensions to be considered a real popup (not a tooltip).
_MIN_POPUP_WIDTH = 200
_MIN_POPUP_HEIGHT = 100

# Minimum time (seconds) a state change must persist to count as an interrupt.
_PERSISTENCE_DELAY_S = 0.2


class DesktopShield:
    """Pre/post-action state comparison for desktop interrupt detection.

    Usage::

        shield = DesktopShield(session_id="desktop-1")

        # Before performing an action:
        shield.capture_pre_action_state()

        # ... perform the action ...
        # ... wait for settle delay ...

        # After the action:
        interrupt = shield.detect_post_action_changes()
        if interrupt is not None:
            # Handle the interrupt (popup, login dialog, etc.)
    """

    def __init__(self, session_id: str) -> None:
        self._session_id = session_id
        self._pre_hwnd: Optional[int] = None
        self._pre_title: str = ""
        self._pre_rect: Optional[tuple[int, int, int, int]] = None
        self._pre_screenshot: Optional[bytes] = None
        self._danger_zone_titles: Optional[list[str]] = None
        self._danger_zone_classes: Optional[list[str]] = None

    def capture_pre_action_state(self, target_hwnd: Optional[int] = None) -> None:
        """Record the current window state before an action.

        Args:
            target_hwnd: The HWND of the target application window.
                If provided, records that window's state instead of the
                foreground window (which might be a terminal).
        """
        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            hwnd = target_hwnd if target_hwnd else user32.GetForegroundWindow()
            if not hwnd or not user32.IsWindow(hwnd):
                hwnd = user32.GetForegroundWindow()
            self._pre_hwnd = hwnd

            # Get window title.
            length = user32.GetWindowTextLengthW(hwnd) + 1
            title_buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(hwnd, title_buf, length)
            self._pre_title = title_buf.value

            # Get window rect.
            rect = wintypes.RECT()
            user32.GetWindowRect(hwnd, ctypes.byref(rect))
            self._pre_rect = (rect.left, rect.top, rect.right, rect.bottom)

        except Exception as exc:
            logger.debug("DesktopShield: failed to capture pre-action state: %s", exc)
            self._pre_hwnd = None
            self._pre_title = ""
            self._pre_rect = None

    def detect_post_action_changes(self) -> Optional[dict]:
        """Compare current state to pre-action state and detect interrupts.

        Returns:
            A dict with interrupt info (``type``, ``description``,
            ``new_title``, ``new_hwnd``) or ``None`` if no interrupt detected.
        """
        if self._pre_hwnd is None:
            return None

        try:
            import ctypes
            from ctypes import wintypes

            user32 = ctypes.windll.user32

            # Brief persistence check — re-check after delay to filter transients.
            time.sleep(_PERSISTENCE_DELAY_S)

            post_hwnd = user32.GetForegroundWindow()

            # Get post-action window title.
            length = user32.GetWindowTextLengthW(post_hwnd) + 1
            title_buf = ctypes.create_unicode_buffer(length)
            user32.GetWindowTextW(post_hwnd, title_buf, length)
            post_title = title_buf.value

            # Get post-action window rect.
            rect = wintypes.RECT()
            user32.GetWindowRect(post_hwnd, ctypes.byref(rect))
            post_width = rect.right - rect.left
            post_height = rect.bottom - rect.top

            # Check 1: Foreground window HWND changed → new window/popup.
            if post_hwnd != self._pre_hwnd:
                # Filter out small windows (tooltips, menus).
                if post_width < _MIN_POPUP_WIDTH or post_height < _MIN_POPUP_HEIGHT:
                    logger.debug(
                        "DesktopShield: new window too small (%dx%d), ignoring.",
                        post_width, post_height,
                    )
                    return None

                logger.info(
                    "DesktopShield: foreground window changed: '%s' -> '%s'",
                    self._pre_title, post_title,
                )
                return {
                    "type": "window_change",
                    "description": (
                        f"New window appeared: '{post_title}' "
                        f"(was: '{self._pre_title}'). "
                        f"Size: {post_width}x{post_height}. "
                        f"This may be a popup, login dialog, or error window."
                    ),
                    "pre_title": self._pre_title,
                    "new_title": post_title,
                    "new_hwnd": post_hwnd,
                }

            # Check 2: Same HWND but title changed significantly → state change.
            if post_title != self._pre_title and self._pre_title:
                # Only fire if the change is significant (not just a counter update).
                if self._is_significant_title_change(self._pre_title, post_title):
                    logger.info(
                        "DesktopShield: window title changed: '%s' -> '%s'",
                        self._pre_title, post_title,
                    )
                    return {
                        "type": "title_change",
                        "description": (
                            f"Window title changed: '{self._pre_title}' -> "
                            f"'{post_title}'. This may indicate a state "
                            f"transition (login prompt, error, navigation)."
                        ),
                        "pre_title": self._pre_title,
                        "new_title": post_title,
                        "new_hwnd": post_hwnd,
                    }

            return None

        except Exception as exc:
            logger.debug("DesktopShield: detection failed: %s", exc)
            return None

    @staticmethod
    def _is_significant_title_change(old_title: str, new_title: str) -> bool:
        """Determine if a title change is significant enough to be an interrupt.

        Filters out trivial changes like counter increments
        (e.g. "(3)" -> "(4)") or minor suffix changes.
        """
        # If one is a substring of the other with minor additions, not significant.
        if old_title in new_title or new_title in old_title:
            len_diff = abs(len(old_title) - len(new_title))
            if len_diff <= 5:
                return False

        # If more than 50% of words changed, it's significant.
        old_words = set(old_title.lower().split())
        new_words = set(new_title.lower().split())
        if not old_words:
            return bool(new_words)
        overlap = len(old_words & new_words)
        return overlap / len(old_words) < 0.5

    # ------------------------------------------------------------------
    # Visual diff for action verification
    # ------------------------------------------------------------------

    def capture_pre_action_screenshot(self, screenshot_bytes: bytes) -> None:
        """Store screenshot bytes captured before an action for later diff."""
        self._pre_screenshot = screenshot_bytes

    def compute_visual_diff(self, post_screenshot: bytes) -> Optional[float]:
        """Compare pre- and post-action screenshots.

        Returns:
            Change ratio (0.0 = identical, 1.0 = completely different).
            ``None`` if comparison cannot be performed.
        """
        if self._pre_screenshot is None:
            return None
        try:
            import io
            from PIL import Image
            import numpy as np

            pre_img = Image.open(io.BytesIO(self._pre_screenshot)).convert("L")
            post_img = Image.open(io.BytesIO(post_screenshot)).convert("L")

            # Resize both to 320px wide for speed.
            target_w = 320
            for img_ref in ("pre", "post"):
                img = pre_img if img_ref == "pre" else post_img
                if img.width > 0:
                    ratio = target_w / img.width
                    new_h = max(1, int(img.height * ratio))
                    img = img.resize((target_w, new_h), Image.NEAREST)
                    if img_ref == "pre":
                        pre_img = img
                    else:
                        post_img = img

            # Ensure same dimensions (post may differ if window resized).
            if pre_img.size != post_img.size:
                post_img = post_img.resize(pre_img.size, Image.NEAREST)

            pre_arr = np.asarray(pre_img, dtype=np.float32)
            post_arr = np.asarray(post_img, dtype=np.float32)
            diff_ratio = float(np.mean(np.abs(pre_arr - post_arr)) / 255.0)
            return diff_ratio

        except Exception as exc:
            logger.debug("DesktopShield: visual diff failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Danger zone safety check
    # ------------------------------------------------------------------

    # Hardcoded danger zone patterns — windows that require user confirmation.
    _DEFAULT_DANGER_TITLES = [
        "付款", "支付", "转账", "payment", "pay", "transfer",
        "用户帐户控制", "user account control",
        "安全警告", "security warning",
        "格式化", "format disk",
        "删除确认", "confirm delete", "permanently delete",
    ]
    _DEFAULT_DANGER_CLASSES = ["#32770"]

    def _load_danger_zone_config(self) -> None:
        """Initialize danger zone patterns (hardcoded defaults)."""
        if self._danger_zone_titles is not None:
            return
        self._danger_zone_titles = list(self._DEFAULT_DANGER_TITLES)
        self._danger_zone_classes = list(self._DEFAULT_DANGER_CLASSES)

    def check_danger_zone(
        self, window_title: str, window_class: Optional[str] = None
    ) -> Optional[str]:
        """Return a warning message if the target is a danger-zone window, else None."""
        self._load_danger_zone_config()
        title_lower = window_title.lower()
        for pattern in (self._danger_zone_titles or []):
            if pattern in title_lower:
                return (
                    f"Blocked: window title '{window_title}' matches danger-zone "
                    f"pattern '{pattern}'. Ask the user for confirmation before "
                    f"interacting with payment, UAC, or deletion dialogs."
                )
        if window_class:
            class_lower = window_class.lower()
            for pattern in (self._danger_zone_classes or []):
                if pattern in class_lower:
                    return (
                        f"Blocked: window class '{window_class}' matches danger-zone "
                        f"pattern '{pattern}'. Ask the user for confirmation."
                    )
        return None
