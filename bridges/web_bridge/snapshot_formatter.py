"""
OpenClaw 2.0 ACI Framework - Snapshot Formatter.

Converts UIDNode arrays into human-readable text snapshots that are
LLM-friendly (like agent-browser's accessibility snapshot format).

The agent sees:
    button "Search" [@e1]
    link "Home" [@e2]
    textbox "Query" [@e3]

Instead of raw JSON UIDNode arrays.
"""
from __future__ import annotations

from typing import Optional
from core.models.schemas import UIDNode


def format_snapshot(elements: list[UIDNode], *, include_bbox: bool = False) -> str:
    """Convert UIDNode list to a text snapshot.

    Args:
        elements: List of UIDNodes from perceive.
        include_bbox: If True, append bbox info (for debugging).

    Returns:
        Multi-line text snapshot, one element per line.
    """
    if not elements:
        return "(empty page - no interactive elements found)"

    lines: list[str] = []
    for el in elements:
        line = _format_node(el, include_bbox=include_bbox)
        if line:
            lines.append(line)

    if not lines:
        return "(no interactive elements)"

    return "\n".join(lines)


def _format_node(el: UIDNode, *, include_bbox: bool = False) -> str:
    """Format a single UIDNode as a text line."""
    # Role/tag
    role = el.role or el.tag or "element"

    # Text (truncate for readability)
    text = (el.text or "").strip()
    if len(text) > 60:
        text = text[:57] + "..."

    # Ref (uid)
    ref = el.uid or "?"

    # Build the line: role "text" [ref=@eN]
    parts = [role]
    if text:
        parts.append(f'"{text}"')

    # Important attributes
    attrs = el.attributes or {}
    attr_parts = []
    if attrs.get("href"):
        href = attrs["href"]
        if len(href) > 40:
            href = href[:37] + "..."
        attr_parts.append(f"href={href}")
    if attrs.get("type"):
        attr_parts.append(f"type={attrs['type']}")
    if attrs.get("expanded") == "false":
        attr_parts.append("collapsed")
    if attrs.get("expanded") == "true":
        attr_parts.append("expanded")
    if attrs.get("checked"):
        attr_parts.append(f"checked={attrs['checked']}")
    if attrs.get("haspopup"):
        attr_parts.append("has-popup")
    if attrs.get("hover_revealed") == "true":
        attr_parts.append("hover-revealed")
    if el.tier == "vision":
        attr_parts.append("vision-detected")
    if not el.interactable:
        attr_parts.append("non-interactive")

    # Bbox (optional)
    if include_bbox and el.bbox:
        attr_parts.append(f"at={el.bbox[0]},{el.bbox[1]}")

    # Compose
    attr_str = ", ".join(attr_parts)
    ref_str = f"[{ref}]"

    if attr_str:
        return f"  {' '.join(parts)} ({attr_str}) {ref_str}"
    else:
        return f"  {' '.join(parts)} {ref_str}"


def format_page_summary(
    url: str,
    title: str,
    elements: list[UIDNode],
    *,
    include_bbox: bool = False,
) -> str:
    """Format a complete page snapshot with header."""
    interactive = [e for e in elements if e.interactable]
    non_interactive = [e for e in elements if not e.interactable]

    parts = [
        f"Page: {title}",
        f"URL: {url}",
        f"Elements: {len(interactive)} interactive, {len(non_interactive)} landmarks",
        "",
        "Interactive:",
        format_snapshot(interactive, include_bbox=include_bbox),
    ]

    if non_interactive:
        parts.extend([
            "",
            "Landmarks:",
            format_snapshot(non_interactive, include_bbox=include_bbox),
        ])

    return "\n".join(parts)
