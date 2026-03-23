"""
OpenClaw 2.0 ACI Framework - UIA Control Tree Extractor (Windows).

Uses the ``uiautomation`` library to walk the Windows UI Automation tree
for the foreground window and return a list of :class:`UIDNode` objects
that the LLM can reference.

Platform guard: raises :class:`NotImplementedError` on non-Windows systems.
"""

from __future__ import annotations

import logging
import platform
import sys
from typing import Optional

from core.models.schemas import UIDNode

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Platform guard
# ---------------------------------------------------------------------------

_IS_WINDOWS = platform.system() == "Windows"

if _IS_WINDOWS:
    try:
        import uiautomation as auto  # type: ignore[import-untyped]
    except ImportError:
        auto = None  # type: ignore[assignment]
        logger.warning(
            "uia_extractor: 'uiautomation' package not installed. "
            "Desktop bridge perception will be unavailable."
        )
else:
    auto = None  # type: ignore[assignment]


def _check_platform() -> None:
    """Raise if not running on Windows or if uiautomation is missing."""
    if not _IS_WINDOWS:
        raise NotImplementedError(
            "UIA control tree extraction is only available on Windows. "
            f"Current platform: {platform.system()}"
        )
    if auto is None:
        raise RuntimeError(
            "The 'uiautomation' package is required for desktop bridge "
            "perception. Install it with: pip install uiautomation"
        )


# ---------------------------------------------------------------------------
# Control type mapping
# ---------------------------------------------------------------------------

# Map UIA ControlType IDs to human-readable tag names that the LLM can
# understand (modelled after HTML equivalents where possible).
_CONTROL_TYPE_MAP: dict[int, str] = {}

if auto is not None:
    _CONTROL_TYPE_MAP = {
        auto.ControlType.ButtonControl: "button",
        auto.ControlType.CalendarControl: "calendar",
        auto.ControlType.CheckBoxControl: "checkbox",
        auto.ControlType.ComboBoxControl: "select",
        auto.ControlType.EditControl: "input",
        auto.ControlType.HyperlinkControl: "a",
        auto.ControlType.ImageControl: "img",
        auto.ControlType.ListControl: "ul",
        auto.ControlType.ListItemControl: "li",
        auto.ControlType.MenuBarControl: "nav",
        auto.ControlType.MenuControl: "menu",
        auto.ControlType.MenuItemControl: "menuitem",
        auto.ControlType.ProgressBarControl: "progress",
        auto.ControlType.RadioButtonControl: "radio",
        auto.ControlType.ScrollBarControl: "scrollbar",
        auto.ControlType.SliderControl: "slider",
        auto.ControlType.SpinnerControl: "spinner",
        auto.ControlType.TabControl: "tablist",
        auto.ControlType.TabItemControl: "tab",
        auto.ControlType.TextControl: "span",
        auto.ControlType.ToolBarControl: "toolbar",
        auto.ControlType.ToolTipControl: "tooltip",
        auto.ControlType.TreeControl: "tree",
        auto.ControlType.TreeItemControl: "treeitem",
        auto.ControlType.WindowControl: "dialog",
        auto.ControlType.DataGridControl: "table",
        auto.ControlType.DocumentControl: "article",
        auto.ControlType.GroupControl: "fieldset",
        auto.ControlType.PaneControl: "section",
    }


# ---------------------------------------------------------------------------
# Extraction logic
# ---------------------------------------------------------------------------

# Control types that the LLM can meaningfully interact with.
_INTERACTABLE_TAGS = {
    "button", "checkbox", "select", "input", "a", "menuitem",
    "radio", "tab", "treeitem", "li", "slider",
}

# Self-drawn renderer classes — UIA cannot see inside these; vision takes over.
_SELF_DRAWN_CLASSES: frozenset[str] = frozenset({
    "MMUIRenderSubWindowHW",       # WeChat message area
    "Chrome_RenderWidgetHostHWND", # Chromium rendered content
    "Internet Explorer_Server",    # IE/Edge legacy renderer
    "MozillaWindowClass",          # Firefox content area
    "Qt5QWindowOwnDCIcon",         # Qt5 custom-drawn
    "DirectUIHWND",                # Windows Explorer detail pane
})

# Maximum recursion depth to prevent runaway tree walking.
_MAX_DEPTH = 15

# Maximum total nodes to collect (performance guardrail).
_MAX_NODES = 500


class UIAExtractor:
    """Extracts the interactable control tree from the Windows foreground window.

    Each call to :meth:`extract` returns a fresh list of :class:`UIDNode`
    objects with sequentially assigned ``oc_`` UIDs.
    """

    def __init__(self) -> None:
        self._uid_counter: int = 0
        self._last_hwnd: Optional[int] = None

    def _next_uid(self) -> str:
        uid = f"oc_{self._uid_counter}"
        self._uid_counter += 1
        return uid

    def extract(self) -> list[UIDNode]:
        """Walk the foreground window's UIA tree and return interactable nodes.

        Returns:
            List of :class:`UIDNode` objects.

        Raises:
            NotImplementedError: If not running on Windows.
            RuntimeError: If uiautomation is not installed.
        """
        _check_platform()

        self._uid_counter = 0
        nodes: list[UIDNode] = []

        try:
            foreground = auto.GetForegroundControl()
            if foreground is None:
                logger.warning("UIAExtractor: no foreground window found.")
                return nodes

            # Store the native HWND for the worker to use.
            try:
                self._last_hwnd = foreground.NativeWindowHandle
            except Exception:
                self._last_hwnd = None

            self._walk(foreground, nodes, depth=0)

        except Exception as exc:
            logger.error("UIAExtractor: extraction failed: %s", exc, exc_info=True)

        logger.debug("UIAExtractor: extracted %d interactable nodes.", len(nodes))
        return nodes

    def get_window_title(self) -> str:
        """Return the title of the current foreground window.

        Returns:
            Window title string, or empty string on failure.
        """
        _check_platform()
        try:
            foreground = auto.GetForegroundControl()
            return foreground.Name if foreground else ""
        except Exception:
            return ""

    def get_last_hwnd(self) -> Optional[int]:
        """Return the native HWND from the most recent ``extract()`` call."""
        return self._last_hwnd

    # ------------------------------------------------------------------
    # Recursive tree walk
    # ------------------------------------------------------------------

    def _walk(
        self,
        control,
        nodes: list[UIDNode],
        depth: int,
    ) -> None:
        """Recursively walk the UIA control tree."""
        if depth > _MAX_DEPTH or len(nodes) >= _MAX_NODES:
            return

        try:
            # Filter out invisible / off-screen controls.
            rect = control.BoundingRectangle
            if rect.width() <= 0 or rect.height() <= 0:
                # Still recurse into children -- a container can be
                # zero-size while its children are visible.
                pass
            else:
                # Determine tag and interactability.
                control_type_id = getattr(control, "ControlType", 0)
                tag = _CONTROL_TYPE_MAP.get(control_type_id, "unknown")
                name = (control.Name or "").strip()[:200]

                is_actionable = tag in _INTERACTABLE_TAGS

                # Also include named, visible controls that aren't in
                # _INTERACTABLE_TAGS — the LLM might still find them
                # useful as context.  But mark them interactable=False
                # so the threshold logic can distinguish real buttons
                # from text containers.
                include = is_actionable
                if not include and name and not getattr(control, "IsOffscreen", True):
                    include = True

                if include and len(nodes) < _MAX_NODES:
                    # Build attributes dict.
                    attrs: dict[str, str] = {}
                    automation_id = getattr(control, "AutomationId", "")
                    if automation_id:
                        attrs["automation-id"] = str(automation_id)
                    class_name = getattr(control, "ClassName", "")
                    if class_name:
                        attrs["class"] = str(class_name)

                    # Extract value for input-like controls.
                    value_pattern = None  # Default so it's always defined
                    try:
                        value_pattern = control.GetValuePattern()
                        if value_pattern:
                            attrs["value"] = str(value_pattern.Value)[:200]
                    except Exception:
                        pass

                    # Extract interactivity metadata for fusion layer.
                    try:
                        invoke_pattern = control.GetInvokePattern()
                        if invoke_pattern is not None:
                            attrs["can_invoke"] = "True"
                    except Exception:
                        pass

                    try:
                        if tag == "input" and value_pattern:
                            is_pw = getattr(control, "CurrentIsPassword", None)
                            if is_pw is None:
                                is_pw = getattr(control, "IsPassword", False)
                            if is_pw:
                                attrs["is_password"] = "True"
                    except Exception:
                        pass

                    if value_pattern is not None:
                        attrs["has_value_pattern"] = "True"

                    # Force non-interactable for self-drawn renderer classes.
                    is_self_drawn = class_name in _SELF_DRAWN_CLASSES
                    if is_self_drawn:
                        is_actionable = False
                        attrs["self_drawn"] = "True"

                    node = UIDNode(
                        uid=self._next_uid(),
                        tag=tag,
                        role=tag,  # UIA ControlType serves as the role.
                        text=name if name else tag,
                        attributes=attrs,
                        bbox=(
                            int(rect.left),
                            int(rect.top),
                            int(rect.width()),
                            int(rect.height()),
                        ),
                        interactable=is_actionable,
                    )
                    nodes.append(node)

        except Exception as exc:
            logger.debug("UIAExtractor: error processing control: %s", exc)

        # Recurse into children.
        try:
            children = control.GetChildren()
            if children:
                for child in children:
                    self._walk(child, nodes, depth + 1)
        except Exception:
            pass  # Some controls don't support child enumeration.
