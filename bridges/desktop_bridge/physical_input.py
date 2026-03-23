"""
OpenClaw 2.0 ACI Framework - Physical Input (Windows).

Hardware-level mouse and keyboard input using ctypes ``SendInput`` on Windows.
All coordinate parameters are in *physical* (screen-absolute) pixels.  The
process declares Per-Monitor DPI Awareness at import time so that
``GetSystemMetrics`` returns physical pixel counts and ``SendInput``
coordinates map correctly without extra DPI scaling.

Platform guard: raises :class:`NotImplementedError` on non-Windows systems.
"""

from __future__ import annotations

import logging
import platform
import time
from typing import Optional

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Windows ctypes imports (guarded)
# ---------------------------------------------------------------------------

if _IS_WINDOWS:
    try:
        import ctypes
        import ctypes.wintypes

        user32 = ctypes.windll.user32
        shcore = None
        try:
            shcore = ctypes.windll.shcore
        except OSError:
            pass  # shcore not available on older Windows versions.

        # --- Constants ---
        INPUT_MOUSE = 0
        INPUT_KEYBOARD = 1
        MOUSEEVENTF_MOVE = 0x0001
        MOUSEEVENTF_LEFTDOWN = 0x0002
        MOUSEEVENTF_LEFTUP = 0x0004
        MOUSEEVENTF_RIGHTDOWN = 0x0008
        MOUSEEVENTF_RIGHTUP = 0x0010
        MOUSEEVENTF_ABSOLUTE = 0x8000
        KEYEVENTF_KEYUP = 0x0002
        KEYEVENTF_UNICODE = 0x0004

        # --- Structures ---
        class MOUSEINPUT(ctypes.Structure):
            _fields_ = [
                ("dx", ctypes.c_long),
                ("dy", ctypes.c_long),
                ("mouseData", ctypes.c_ulong),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class KEYBDINPUT(ctypes.Structure):
            _fields_ = [
                ("wVk", ctypes.c_ushort),
                ("wScan", ctypes.c_ushort),
                ("dwFlags", ctypes.c_ulong),
                ("time", ctypes.c_ulong),
                ("dwExtraInfo", ctypes.POINTER(ctypes.c_ulong)),
            ]

        class HARDWAREINPUT(ctypes.Structure):
            _fields_ = [
                ("uMsg", ctypes.c_ulong),
                ("wParamL", ctypes.c_ushort),
                ("wParamH", ctypes.c_ushort),
            ]

        class _INPUT_UNION(ctypes.Union):
            _fields_ = [
                ("mi", MOUSEINPUT),
                ("ki", KEYBDINPUT),
                ("hi", HARDWAREINPUT),
            ]

        class INPUT(ctypes.Structure):
            _fields_ = [
                ("type", ctypes.c_ulong),
                ("union", _INPUT_UNION),
            ]

        _CTYPES_AVAILABLE = True

    except Exception as exc:
        logger.warning("physical_input: ctypes setup failed: %s", exc)
        _CTYPES_AVAILABLE = False
else:
    _CTYPES_AVAILABLE = False

# Declare process as Per-Monitor DPI Aware so GetSystemMetrics returns
# physical pixels and SendInput absolute coordinates are correct.
if _IS_WINDOWS and _CTYPES_AVAILABLE:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)  # PROCESS_PER_MONITOR_DPI_AWARE
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()  # Vista fallback
        except Exception:
            pass

# ---------------------------------------------------------------------------
# Virtual key code mapping (subset of common keys)
# ---------------------------------------------------------------------------

_VK_MAP: dict[str, int] = {
    "enter": 0x0D,
    "return": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "esc": 0x1B,
    "backspace": 0x08,
    "delete": 0x2E,
    "space": 0x20,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "f1": 0x70, "f2": 0x71, "f3": 0x72, "f4": 0x73,
    "f5": 0x74, "f6": 0x75, "f7": 0x76, "f8": 0x77,
    "f9": 0x78, "f10": 0x79, "f11": 0x7A, "f12": 0x7B,
    "ctrl": 0x11, "control": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    "win": 0x5B, "windows": 0x5B,
    "capslock": 0x14,
    "numlock": 0x90,
    "printscreen": 0x2C,
    "insert": 0x2D,
}


def _check_platform() -> None:
    """Raise if not on Windows or ctypes is unavailable."""
    if not _IS_WINDOWS:
        raise NotImplementedError(
            f"Physical input is only available on Windows. "
            f"Current platform: {platform.system()}"
        )
    if not _CTYPES_AVAILABLE:
        raise RuntimeError(
            "ctypes-based input is not available. Check Windows permissions."
        )


# ---------------------------------------------------------------------------
# DPI scaling
# ---------------------------------------------------------------------------

def _get_dpi_scale() -> float:
    """Query the system DPI and return a scale factor (1.0 = 96 DPI).

    Falls back to 1.0 if the DPI cannot be determined.
    """
    if not _IS_WINDOWS or not _CTYPES_AVAILABLE:
        return 1.0

    try:
        # Try per-monitor DPI awareness (Windows 8.1+).
        if shcore is not None:
            dpi_x = ctypes.c_uint()
            dpi_y = ctypes.c_uint()
            # MDT_EFFECTIVE_DPI = 0
            monitor = user32.MonitorFromPoint(
                ctypes.wintypes.POINT(0, 0), 1  # MONITOR_DEFAULTTOPRIMARY
            )
            hr = shcore.GetDpiForMonitor(
                monitor, 0, ctypes.byref(dpi_x), ctypes.byref(dpi_y)
            )
            if hr == 0 and dpi_x.value > 0:
                return dpi_x.value / 96.0
    except Exception:
        pass

    try:
        # Fallback: system-wide DPI via GetDeviceCaps.
        hdc = user32.GetDC(0)
        if hdc:
            gdi32 = ctypes.windll.gdi32
            dpi = gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX = 88
            user32.ReleaseDC(0, hdc)
            if dpi > 0:
                return dpi / 96.0
    except Exception:
        pass

    return 1.0


def _to_absolute_coords(x: int, y: int) -> tuple[int, int]:
    """Convert physical screen coordinates to SendInput absolute [0, 65535].

    All callers provide screen-absolute *physical* pixel coordinates
    (from UIA ``BoundingRectangle`` or offset-corrected vision bboxes).
    The process is declared Per-Monitor DPI Aware at module load, so
    ``GetSystemMetrics`` returns physical pixel counts — no additional
    DPI scaling is needed.
    """
    screen_w = user32.GetSystemMetrics(0)  # SM_CXSCREEN (physical px)
    screen_h = user32.GetSystemMetrics(1)  # SM_CYSCREEN (physical px)

    if screen_w <= 0 or screen_h <= 0:
        return (0, 0)

    abs_x = int(x * 65535 / screen_w)
    abs_y = int(y * 65535 / screen_h)
    return (abs_x, abs_y)


# ---------------------------------------------------------------------------
# Low-level input helpers
# ---------------------------------------------------------------------------

def _send_inputs(*inputs: "INPUT") -> int:
    """Send one or more INPUT structures via SendInput."""
    n = len(inputs)
    arr = (INPUT * n)(*inputs)
    return user32.SendInput(n, arr, ctypes.sizeof(INPUT))


def _make_mouse_input(
    dx: int = 0,
    dy: int = 0,
    flags: int = 0,
    data: int = 0,
) -> "INPUT":
    """Construct an INPUT structure for a mouse event."""
    mi = MOUSEINPUT(
        dx=dx,
        dy=dy,
        mouseData=data,
        dwFlags=flags,
        time=0,
        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)),
    )
    inp = INPUT(type=INPUT_MOUSE)
    inp.union.mi = mi
    return inp


def _make_key_input(
    vk: int = 0,
    scan: int = 0,
    flags: int = 0,
) -> "INPUT":
    """Construct an INPUT structure for a keyboard event."""
    ki = KEYBDINPUT(
        wVk=vk,
        wScan=scan,
        dwFlags=flags,
        time=0,
        dwExtraInfo=ctypes.pointer(ctypes.c_ulong(0)),
    )
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.union.ki = ki
    return inp


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def click(x: int, y: int) -> None:
    """Move to (x, y) in logical pixels and perform a left-click.

    Args:
        x: Horizontal position (logical pixels, pre-DPI-scaling).
        y: Vertical position (logical pixels, pre-DPI-scaling).
    """
    _check_platform()
    abs_x, abs_y = _to_absolute_coords(x, y)
    flags = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE

    _send_inputs(
        _make_mouse_input(dx=abs_x, dy=abs_y, flags=flags),
        _make_mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
        _make_mouse_input(flags=MOUSEEVENTF_LEFTUP),
    )
    logger.debug("physical_input: click(%d, %d) -> abs(%d, %d)", x, y, abs_x, abs_y)


def double_click(x: int, y: int) -> None:
    """Move to (x, y) and perform a double left-click.

    Args:
        x: Horizontal position (logical pixels).
        y: Vertical position (logical pixels).
    """
    _check_platform()
    abs_x, abs_y = _to_absolute_coords(x, y)
    flags_move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE

    _send_inputs(
        _make_mouse_input(dx=abs_x, dy=abs_y, flags=flags_move),
        _make_mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
        _make_mouse_input(flags=MOUSEEVENTF_LEFTUP),
    )
    time.sleep(0.05)
    _send_inputs(
        _make_mouse_input(flags=MOUSEEVENTF_LEFTDOWN),
        _make_mouse_input(flags=MOUSEEVENTF_LEFTUP),
    )
    logger.debug("physical_input: double_click(%d, %d)", x, y)


def right_click(x: int, y: int) -> None:
    """Move to (x, y) and perform a right-click.

    Args:
        x: Horizontal position (logical pixels).
        y: Vertical position (logical pixels).
    """
    _check_platform()
    abs_x, abs_y = _to_absolute_coords(x, y)
    flags_move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE

    _send_inputs(
        _make_mouse_input(dx=abs_x, dy=abs_y, flags=flags_move),
        _make_mouse_input(flags=MOUSEEVENTF_RIGHTDOWN),
        _make_mouse_input(flags=MOUSEEVENTF_RIGHTUP),
    )
    logger.debug("physical_input: right_click(%d, %d)", x, y)


def type_text(text: str) -> None:
    """Type a string of text character-by-character using Unicode input events.

    Args:
        text: The text to type.
    """
    _check_platform()
    for char in text:
        scan = ord(char)
        _send_inputs(
            _make_key_input(scan=scan, flags=KEYEVENTF_UNICODE),
            _make_key_input(scan=scan, flags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        )
        time.sleep(0.01)  # Brief delay between characters.
    logger.debug("physical_input: type_text(%d chars)", len(text))


def press_key(key: str) -> None:
    """Press and release a single key.

    Supports named keys (e.g. ``"enter"``, ``"tab"``, ``"f5"``) and
    single characters.  For modifier combos, use :func:`press_combo`.

    Args:
        key: Key name or single character.
    """
    _check_platform()
    key_lower = key.lower().strip()

    vk = _VK_MAP.get(key_lower)
    if vk is not None:
        _send_inputs(
            _make_key_input(vk=vk),
            _make_key_input(vk=vk, flags=KEYEVENTF_KEYUP),
        )
    elif len(key) == 1:
        # Single character -- use VkKeyScan.
        vk_scan = user32.VkKeyScanW(ord(key))
        vk_code = vk_scan & 0xFF
        shift_needed = (vk_scan >> 8) & 0x01

        if shift_needed:
            _send_inputs(_make_key_input(vk=0x10))  # Shift down
        _send_inputs(
            _make_key_input(vk=vk_code),
            _make_key_input(vk=vk_code, flags=KEYEVENTF_KEYUP),
        )
        if shift_needed:
            _send_inputs(_make_key_input(vk=0x10, flags=KEYEVENTF_KEYUP))
    else:
        logger.warning("physical_input: unknown key '%s'", key)
        raise ValueError(f"Unknown key: {key!r}")

    logger.debug("physical_input: press_key('%s')", key)


def press_combo(*keys: str) -> None:
    """Press a keyboard combination (e.g. Ctrl+C, Alt+F4).

    Args:
        keys: Sequence of key names, e.g. ``("ctrl", "c")``.
    """
    _check_platform()
    vk_codes: list[int] = []

    for key in keys:
        key_lower = key.lower().strip()
        vk = _VK_MAP.get(key_lower)
        if vk is None and len(key) == 1:
            vk = user32.VkKeyScanW(ord(key)) & 0xFF
        if vk is None:
            raise ValueError(f"Unknown key in combo: {key!r}")
        vk_codes.append(vk)

    # Press all keys down in order.
    for vk in vk_codes:
        _send_inputs(_make_key_input(vk=vk))

    # Release in reverse order.
    for vk in reversed(vk_codes):
        _send_inputs(_make_key_input(vk=vk, flags=KEYEVENTF_KEYUP))

    logger.debug("physical_input: press_combo(%s)", "+".join(keys))


def move_to(x: int, y: int) -> None:
    """Move the mouse cursor to (x, y) without clicking.

    Args:
        x: Horizontal position (logical pixels).
        y: Vertical position (logical pixels).
    """
    _check_platform()
    abs_x, abs_y = _to_absolute_coords(x, y)
    _send_inputs(
        _make_mouse_input(
            dx=abs_x, dy=abs_y,
            flags=MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE,
        ),
    )
    logger.debug("physical_input: move_to(%d, %d)", x, y)
