"""
OpenClaw 2.0 ACI Framework - Cursor Mutation Probing (Desktop Only).

Scans a target window by moving the cursor across a grid and reading back
the system cursor shape.  Cursor shape changes reveal OS-level ground truth
about interactability:

    IDC_HAND   → clickable (link / button)
    IDC_IBEAM  → text input field
    IDC_ARROW  → non-interactable background
    custom     → likely interactable (mark for tooltip probe)

Algorithm:
    1. Build NxM grid over target window.
    2. For each point: SetCursorPos → sleep(1ms) → GetCursorInfo → record.
    3. Cluster adjacent same-type non-arrow points into bounding boxes.
    4. For unknown/custom cursor clusters: hover center 500ms → read tooltip.
    5. Return DetectedElement list.

Typical timing: ~350ms for 300 points (20×15 grid).

Platform: Windows only.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import logging
import platform
import time
from typing import Any, Callable, Optional

from core.detection_tier import DetectedElement, DetectionTier, TierResult

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# Win32 constants — standard cursor handles
# ---------------------------------------------------------------------------

# These are resource IDs loaded via LoadCursor(NULL, IDC_*).
# We resolve them once at import time.
_CURSOR_MAP: dict[int, str] = {}

if _IS_WINDOWS:
    _user32 = ctypes.windll.user32  # type: ignore[attr-defined]

    class CURSORINFO(ctypes.Structure):
        _fields_ = [
            ("cbSize", ctypes.wintypes.DWORD),
            ("flags", ctypes.wintypes.DWORD),
            ("hCursor", ctypes.wintypes.HANDLE),
            ("ptScreenPos", ctypes.wintypes.POINT),
        ]

    class RECT(ctypes.Structure):
        _fields_ = [
            ("left", ctypes.c_long),
            ("top", ctypes.c_long),
            ("right", ctypes.c_long),
            ("bottom", ctypes.c_long),
        ]

    # Standard cursor resource IDs (MAKEINTRESOURCE values).
    _IDC_ARROW = 32512
    _IDC_IBEAM = 32513
    _IDC_WAIT = 32514
    _IDC_CROSS = 32515
    _IDC_UPARROW = 32516
    _IDC_SIZENWSE = 32642
    _IDC_SIZENESW = 32643
    _IDC_SIZEWE = 32644
    _IDC_SIZENS = 32645
    _IDC_SIZEALL = 32646
    _IDC_NO = 32648
    _IDC_HAND = 32649
    _IDC_APPSTARTING = 32650

    # Load each standard cursor → map handle to name.
    for _name, _idc in [
        ("ARROW", _IDC_ARROW),
        ("IBEAM", _IDC_IBEAM),
        ("WAIT", _IDC_WAIT),
        ("CROSS", _IDC_CROSS),
        ("UPARROW", _IDC_UPARROW),
        ("SIZENWSE", _IDC_SIZENWSE),
        ("SIZENESW", _IDC_SIZENESW),
        ("SIZEWE", _IDC_SIZEWE),
        ("SIZENS", _IDC_SIZENS),
        ("SIZEALL", _IDC_SIZEALL),
        ("NO", _IDC_NO),
        ("HAND", _IDC_HAND),
        ("APPSTARTING", _IDC_APPSTARTING),
    ]:
        _h = _user32.LoadCursorW(0, _idc)
        if _h:
            _CURSOR_MAP[_h] = _name

    # Cursor types we consider interactable.
    _INTERACTABLE_CURSORS = {"HAND", "IBEAM"}

    # Cursor types that are resize handles — skip.
    _RESIZE_CURSORS = {"SIZENWSE", "SIZENESW", "SIZEWE", "SIZENS", "SIZEALL"}

    # Cursor types that mean "not interactable".
    _SKIP_CURSORS = {"ARROW", "WAIT", "APPSTARTING", "NO"} | _RESIZE_CURSORS


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _get_cursor_handle() -> int:
    """Return the current system cursor handle."""
    ci = CURSORINFO()
    ci.cbSize = ctypes.sizeof(CURSORINFO)
    _user32.GetCursorInfo(ctypes.byref(ci))
    return ci.hCursor


def _get_window_rect(hwnd: int) -> tuple[int, int, int, int]:
    """Return (left, top, width, height) of a window."""
    rect = RECT()
    _user32.GetWindowRect(hwnd, ctypes.byref(rect))
    return (
        rect.left,
        rect.top,
        rect.right - rect.left,
        rect.bottom - rect.top,
    )


def _classify_cursor(handle: int) -> tuple[str, bool]:
    """Classify a cursor handle.

    Returns:
        (cursor_name, is_interactable)
    """
    name = _CURSOR_MAP.get(handle)
    if name is None:
        # Custom cursor → likely interactable (app-specific)
        return ("CUSTOM", True)
    if name in _INTERACTABLE_CURSORS:
        return (name, True)
    return (name, False)


def _cursor_to_tag(cursor_name: str) -> str:
    """Map cursor name to a UIDNode-compatible tag."""
    if cursor_name == "HAND":
        return "link"
    if cursor_name == "IBEAM":
        return "input"
    if cursor_name == "CROSS":
        return "canvas"
    return "unknown"


# ---------------------------------------------------------------------------
# Clustering — group adjacent interactable points into bounding boxes
# ---------------------------------------------------------------------------

def _cluster_points(
    points: list[tuple[int, int, str]],  # (x, y, cursor_name)
    merge_gap: int = 60,
) -> list[dict]:
    """Cluster adjacent same-type points into bounding boxes.

    Uses a Manhattan-distance based greedy merge to group points that 
    likely belong to the same UI element (like a search bar).
    """
    if not points:
        return []

    # Group by cursor type.
    by_type: dict[str, list[tuple[int, int]]] = {}
    for x, y, ctype in points:
        by_type.setdefault(ctype, []).append((x, y))

    clusters: list[dict] = []

    for ctype, pts in by_type.items():
        # Sort points to process them spatially.
        pts.sort(key=lambda p: (p[1], p[0]))

        active: list[list[tuple[int, int]]] = []

        for px, py in pts:
            merged = False
            for cluster in active:
                # If point is near ANY point in the cluster, merge it.
                # 60px gap covers typical control spacing on high-DPI screens.
                if any(abs(px - cx) <= merge_gap and abs(py - cy) <= merge_gap for cx, cy in cluster):
                    cluster.append((px, py))
                    merged = True
                    break

            if not merged:
                active.append([(px, py)])

        # Convert point clusters to bounding boxes.
        for cluster in active:
            xs = [p[0] for p in cluster]
            ys = [p[1] for p in cluster]
            x_min, x_max = min(xs), max(xs)
            y_min, y_max = min(ys), max(ys)
            
            # Padding based on grid density.
            pad = 10
            clusters.append({
                "bbox": (
                    max(0, x_min - pad),
                    max(0, y_min - pad),
                    (x_max - x_min) + 2 * pad,
                    (y_max - y_min) + 2 * pad,
                ),
                "cursor_type": ctype,
                "points": cluster,
            })

    return clusters


# ---------------------------------------------------------------------------
# Tooltip detection via UIA
# ---------------------------------------------------------------------------

def _read_tooltip_at(x: int, y: int, hover_ms: int = 500) -> Optional[str]:
    """Hover at a point and try to read a tooltip via UIA.

    Returns tooltip text or None.
    """
    try:
        import comtypes.client  # type: ignore[import-untyped]
        from comtypes.gen.UIAutomationClient import IUIAutomation  # type: ignore[import-untyped]

        _user32.SetCursorPos(x, y)
        time.sleep(hover_ms / 1000.0)

        uia = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=IUIAutomation,
        )
        element = uia.ElementFromPoint(ctypes.wintypes.POINT(x, y))
        if element:
            name = element.CurrentName
            if name and name.strip():
                return name.strip()
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Detection Tier
# ---------------------------------------------------------------------------

class CursorProbe(DetectionTier):
    """Win32 cursor mutation probing tier."""

    def __init__(
        self,
        *,
        target_hwnd_getter: Optional[Callable[[], Optional[int]]] = None,
        grid_cols: int = 20,
        grid_rows: int = 15,
        delay_ms: float = 1.0,
        tooltip_hover_ms: int = 500,
        merge_gap: int = 30,
    ) -> None:
        self._hwnd_getter = target_hwnd_getter
        self._grid_cols = grid_cols
        self._grid_rows = grid_rows
        self._delay_s = delay_ms / 1000.0
        self._tooltip_hover_ms = tooltip_hover_ms
        self._merge_gap = merge_gap

    @property
    def name(self) -> str:
        return "cursor_probe"

    @property
    def priority(self) -> float:
        return 1.0

    def is_available(self) -> bool:
        return _IS_WINDOWS

    def detect(
        self,
        screenshot_bytes: bytes,
        existing_elements: list[DetectedElement],
        *,
        roi: Optional[tuple[int, int, int, int]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> TierResult:
        if not _IS_WINDOWS:
            return TierResult(elements=[], source_name=self.name)

        # Determine scan area.
        hwnd = None
        if self._hwnd_getter:
            hwnd = self._hwnd_getter()

        if roi:
            win_x, win_y, win_w, win_h = roi
        elif hwnd:
            win_x, win_y, win_w, win_h = _get_window_rect(hwnd)
        else:
            # Fallback: use foreground window.
            fg = _user32.GetForegroundWindow()
            if fg:
                win_x, win_y, win_w, win_h = _get_window_rect(fg)
            else:
                return TierResult(elements=[], source_name=self.name)

        # Save original cursor position.
        orig = ctypes.wintypes.POINT()
        _user32.GetCursorPos(ctypes.byref(orig))

        # Build grid.
        step_x = max(1, win_w // self._grid_cols)
        step_y = max(1, win_h // self._grid_rows)

        interactable_points: list[tuple[int, int, str]] = []

        try:
            for row in range(self._grid_rows):
                for col in range(self._grid_cols):
                    px = win_x + col * step_x + step_x // 2
                    py = win_y + row * step_y + step_y // 2

                    _user32.SetCursorPos(px, py)
                    # Increased delay to 20ms to allow custom UI (like WeChat) to update 
                    # the cursor shape before we sample it.
                    time.sleep(0.02)

                    handle = _get_cursor_handle()
                    cursor_name, is_interact = _classify_cursor(handle)

                    if is_interact:
                        interactable_points.append((px, py, cursor_name))
        finally:
            # Restore cursor.
            _user32.SetCursorPos(orig.x, orig.y)

        if not interactable_points:
            return TierResult(elements=[], source_name=self.name)

        # Cluster points into elements.
        clusters = _cluster_points(interactable_points, self._merge_gap)

        elements: list[DetectedElement] = []
        for cl in clusters:
            bbox = cl["bbox"]
            ctype = cl["cursor_type"]
            tag = _cursor_to_tag(ctype)
            label = ""

            # For custom/unknown cursors and HAND (clickable buttons), try tooltip detection.
            if ctype in ("CUSTOM", "HAND") and self._tooltip_hover_ms > 0:
                center_x = bbox[0] + bbox[2] // 2
                center_y = bbox[1] + bbox[3] // 2
                tip = _read_tooltip_at(center_x, center_y, self._tooltip_hover_ms)
                if tip:
                    label = tip
                    tag = "button"  # tooltip suggests it's a labeled control

            # Restore cursor after each tooltip probe.
            _user32.SetCursorPos(orig.x, orig.y)

            elements.append(DetectedElement(
                bbox=bbox,
                label=label,
                tag=tag,
                interactable=True,
                confidence=0.8 if ctype in ("HAND", "IBEAM") else 0.6,
                cursor_type=ctype,
                needs_contour=(ctype == "CUSTOM" and not label),
            ))

        return TierResult(elements=elements, source_name=self.name)


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _test() -> None:
    """Quick test: scan the foreground window and print results."""
    import json

    probe = CursorProbe(grid_cols=20, grid_rows=15, delay_ms=1, tooltip_hover_ms=0)

    print("Cursor Probe Test — switch to a target window within 3 seconds...")
    time.sleep(3)

    result = probe.detect(b"", [])

    print(f"\nFound {len(result.elements)} interactable regions in {result.elapsed_ms:.1f}ms:")
    for i, elem in enumerate(result.elements):
        print(f"  [{i}] bbox={elem.bbox} tag={elem.tag} cursor={elem.cursor_type} label={elem.label!r}")

    # Also dump as JSON for debugging.
    print(f"\nJSON:")
    data = [
        {"bbox": list(e.bbox), "tag": e.tag, "cursor_type": e.cursor_type, "label": e.label}
        for e in result.elements
    ]
    print(json.dumps(data, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    _test()
