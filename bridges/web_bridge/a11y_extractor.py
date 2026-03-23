"""
OpenClaw 2.0 ACI Framework - T0 Accessibility Tree Extractor.

Extracts interactive elements from the browser's accessibility tree via
Chrome DevTools Protocol. Uses CDP Accessibility.getFullAXTree for
structured node data and DOM.getBoxModel for bounding boxes.
"""
from __future__ import annotations

import asyncio
import logging
import os
from typing import Any, Optional

from core.models.schemas import UIDNode

logger = logging.getLogger(__name__)

_T0_MAX = int(os.environ.get("OPENCLAW_WEB_T0_MAX", "300"))
_VIEWPORT_MARGIN = 100

_INTERACTIVE_ROLES: frozenset[str] = frozenset({
    "button", "link", "textbox", "checkbox", "radio", "combobox",
    "slider", "spinbutton", "switch", "tab", "menuitem", "option",
    "searchbox", "treeitem", "gridcell", "listbox", "menu", "menubar",
    "heading", "landmark", "banner", "navigation", "main", "contentinfo",
})


def _quad_to_bbox(quad: list[float]) -> tuple[int, int, int, int]:
    """Convert a CDP quad (8 floats: 4 corners) to (x, y, w, h)."""
    x1, y1 = quad[0], quad[1]
    x2, y2 = quad[4], quad[5]
    return (int(x1), int(y1), int(x2 - x1), int(y2 - y1))


def _is_in_viewport(bbox: tuple[int, int, int, int], vw: int, vh: int) -> bool:
    """Return True if the bbox overlaps the viewport (with margin)."""
    x, y, w, h = bbox
    if x + w < -_VIEWPORT_MARGIN or y + h < -_VIEWPORT_MARGIN:
        return False
    if x > vw + _VIEWPORT_MARGIN or y > vh + _VIEWPORT_MARGIN:
        return False
    return True


def _prioritize(nodes: list[UIDNode], max_count: int) -> list[UIDNode]:
    """Smart prioritization: interactive controls first, then landmarks/headings, then rest."""
    if len(nodes) <= max_count:
        return nodes
    interactive = []
    headings = []
    others = []
    for n in nodes:
        if n.role in ("heading", "landmark", "banner", "navigation", "main", "contentinfo"):
            headings.append(n)
        elif n.role in _INTERACTIVE_ROLES:
            interactive.append(n)
        else:
            others.append(n)
    result = interactive[:max_count]
    remaining = max_count - len(result)
    if remaining > 0:
        result.extend(headings[:remaining])
        remaining = max_count - len(result)
    if remaining > 0:
        result.extend(others[:remaining])
    return result[:max_count]


class A11yExtractor:
    """T0 extractor: pulls the full accessibility tree via CDP and resolves bounding boxes."""

    def __init__(self, viewport_width: int = 1280, viewport_height: int = 900) -> None:
        self._vw = viewport_width
        self._vh = viewport_height

    async def extract(self, cdp) -> list[UIDNode]:
        """Extract interactive UIDNodes from the browser's accessibility tree.

        Args:
            cdp: A CDP session object with an async ``send(method, params)`` interface.

        Returns:
            List of UIDNode instances for interactive elements visible in the viewport.
        """
        try:
            tree = await cdp.send("Accessibility.getFullAXTree")
        except Exception as exc:
            logger.error("A11yExtractor: failed to get AX tree: %s", exc)
            return []

        ax_nodes = tree.get("nodes", [])
        filtered: list[dict[str, Any]] = []
        for node in ax_nodes:
            role_val = (node.get("role") or {}).get("value", "")
            if role_val in _INTERACTIVE_ROLES and node.get("backendDOMNodeId"):
                filtered.append(node)
        if not filtered:
            return []

        async def _get_bbox(node: dict) -> Optional[tuple[dict, tuple[int, int, int, int]]]:
            try:
                box = await cdp.send("DOM.getBoxModel", {"backendNodeId": node["backendDOMNodeId"]})
                quad = box.get("model", {}).get("content", [])
                if len(quad) >= 8:
                    return (node, _quad_to_bbox(quad))
            except Exception:
                pass
            return None

        results = await asyncio.gather(*[_get_bbox(n) for n in filtered], return_exceptions=True)

        uid_nodes: list[UIDNode] = []
        for r in results:
            if isinstance(r, Exception) or r is None:
                continue
            node, bbox = r
            if not _is_in_viewport(bbox, self._vw, self._vh):
                continue
            role_val = (node.get("role") or {}).get("value", "")
            name_val = (node.get("name") or {}).get("value", "")
            attrs: dict[str, str] = {}
            for prop in node.get("properties", []):
                pname = prop.get("name", "")
                pval = prop.get("value", {}).get("value", "")
                if pname and pval is not None:
                    attrs[pname] = str(pval)
            is_interactive = role_val not in (
                "heading", "landmark", "banner", "navigation", "main", "contentinfo",
            )
            uid_nodes.append(UIDNode(
                uid="_pending",
                tag=role_val,
                role=role_val,
                text=str(name_val)[:200],
                attributes=attrs,
                bbox=bbox,
                interactable=is_interactive,
                tier="a11y",
            ))
        return _prioritize(uid_nodes, _T0_MAX)
