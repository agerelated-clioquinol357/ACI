"""
OpenClaw 2.0 ACI Framework - Perception Fusion Module.

Merges UIA (oc_) and vision (vc_) detection results into a unified element
tree using spatial fusion, interactivity weighting, and IoU deduplication.

Feature flag: OPENCLAW_PERCEPTION_FUSION (default "1", set "0" to disable).
"""

from __future__ import annotations

import logging
import os
from typing import Optional

from core.models.schemas import UIDNode

logger = logging.getLogger(__name__)

_FUSION_ENABLED = os.environ.get("OPENCLAW_PERCEPTION_FUSION", "1") != "0"

# IoU threshold above which two elements are considered "the same".
_IOU_THRESHOLD = 0.7

# Containment margin in pixels (handles slight misalignment).
_CONTAINMENT_MARGIN = 5

# Tags that are pure layout containers — not meaningfully interactable.
# Aligned with _CONTROL_TYPE_MAP in uia_extractor.py.
CONTAINER_TAGS = {
    "section",    # PaneControl
    "fieldset",   # GroupControl
    "dialog",     # WindowControl
    "article",    # DocumentControl
    "span",       # TextControl
    "toolbar",    # ToolBarControl
    "nav",        # MenuBarControl
    "tree",       # TreeControl
    "tablist",    # TabControl
    "ul",         # ListControl
    "table",      # DataGridControl
    "unknown",    # unmapped types
}


def _is_contained(
    inner_bbox: tuple[int, int, int, int],
    outer_bbox: tuple[int, int, int, int],
    margin: int = _CONTAINMENT_MARGIN,
) -> bool:
    """Check if inner bbox is fully within outer bbox (±margin tolerance)."""
    ix, iy, iw, ih = inner_bbox
    ox, oy, ow, oh = outer_bbox
    return (
        ix >= ox - margin
        and iy >= oy - margin
        and ix + iw <= ox + ow + margin
        and iy + ih <= oy + oh + margin
    )


def _iou(
    bbox_a: tuple[int, int, int, int],
    bbox_b: tuple[int, int, int, int],
) -> float:
    """Compute intersection-over-union of two (x, y, w, h) bboxes."""
    ax, ay, aw, ah = bbox_a
    bx, by, bw, bh = bbox_b

    if aw <= 0 or ah <= 0 or bw <= 0 or bh <= 0:
        return 0.0

    # Convert to (x1, y1, x2, y2).
    ax2, ay2 = ax + aw, ay + ah
    bx2, by2 = bx + bw, by + bh

    # Intersection.
    ix1 = max(ax, bx)
    iy1 = max(ay, by)
    ix2 = min(ax2, bx2)
    iy2 = min(ay2, by2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    intersection = (ix2 - ix1) * (iy2 - iy1)
    union = aw * ah + bw * bh - intersection
    if union <= 0:
        return 0.0
    return intersection / union


class PerceptionFusion:
    """Merges UIA and vision detection results using spatial analysis."""

    def merge(
        self,
        uia_nodes: list[UIDNode],
        vision_nodes: list[UIDNode],
    ) -> list[UIDNode]:
        """Merge UIA (oc_) and vision (vc_) nodes into a unified list.

        Rules applied in order:
        1. For each vc_, find smallest containing oc_ that is a non-interactable
           container → promote vc_, attach parent_oc hint.
        2. For overlapping (IoU > 0.7) oc_+vc_ where oc_ is interactable →
           merge into single node keeping oc_ UID.
        3. If oc_ has is_password or can_invoke → set prefer_uia=true.
        4. Non-overlapping nodes from both sources kept as-is.
        """
        if not _FUSION_ENABLED:
            return list(uia_nodes) + list(vision_nodes)

        if not vision_nodes:
            return list(uia_nodes)
        if not uia_nodes:
            return list(vision_nodes)

        result: list[UIDNode] = []
        matched_vc: set[str] = set()
        matched_oc: set[str] = set()

        oc_with_bbox = [(n, n.bbox) for n in uia_nodes if n.bbox]

        for vc in vision_nodes:
            if not vc.bbox:
                result.append(vc)
                matched_vc.add(vc.uid)
                continue

            vc_bbox = vc.bbox
            best_container: Optional[UIDNode] = None
            best_container_area = float("inf")
            best_overlap: Optional[UIDNode] = None
            best_iou = 0.0

            for oc, oc_bbox in oc_with_bbox:
                # Skip self-drawn containers — vision sees inside, UIA doesn't.
                if oc.attributes.get("self_drawn") == "True":
                    continue

                iou_val = _iou(vc_bbox, oc_bbox)

                if iou_val > best_iou:
                    best_iou = iou_val
                    best_overlap = oc

                if (_is_contained(vc_bbox, oc_bbox)
                        and oc.tag in CONTAINER_TAGS
                        and not oc.interactable):
                    area = oc_bbox[2] * oc_bbox[3]
                    if area < best_container_area:
                        best_container_area = area
                        best_container = oc

            # Decision: container promotion.
            if best_container and best_iou < _IOU_THRESHOLD:
                new_attrs = dict(vc.attributes)
                new_attrs["parent_oc"] = best_container.uid
                result.append(UIDNode(
                    uid=vc.uid, tag=vc.tag, role=vc.role, text=vc.text,
                    bbox=vc.bbox, interactable=vc.interactable,
                    attributes=new_attrs, tier=vc.tier,
                ))
                matched_vc.add(vc.uid)
                continue

            # Decision: IoU merge.
            if best_overlap and best_iou >= _IOU_THRESHOLD:
                oc = best_overlap
                is_prefer_uia = (
                    oc.attributes.get("is_password") == "True"
                    or oc.attributes.get("can_invoke") == "True"
                    or oc.attributes.get("has_value_pattern") == "True"
                )

                merged_attrs = dict(oc.attributes)
                vc_conf = vc.attributes.get("confidence", "")
                if vc_conf:
                    merged_attrs["visual_confidence"] = vc_conf
                if is_prefer_uia:
                    merged_attrs["prefer_uia"] = "true"

                merged_text = oc.text if oc.text and oc.text != oc.tag else vc.text

                result.append(UIDNode(
                    uid=oc.uid,
                    tag=oc.tag,
                    role=oc.role,
                    text=merged_text,
                    bbox=oc.bbox,
                    interactable=oc.interactable or vc.interactable,
                    attributes=merged_attrs,
                    tier="fused",
                ))
                matched_vc.add(vc.uid)
                matched_oc.add(oc.uid)
                continue

            # No match — keep vc_ as-is.
            result.append(vc)
            matched_vc.add(vc.uid)

        # Add unmatched oc_ nodes.
        for oc in uia_nodes:
            if oc.uid not in matched_oc:
                result.append(oc)

        return result


_GATEKEEPER_ENABLED = os.environ.get("OPENCLAW_VISUAL_GATEKEEPER", "1") != "0"

# Visual gatekeeper thresholds.
_VARIANCE_THRESHOLD = 100.0
_EDGE_DENSITY_THRESHOLD = 0.05


def visual_gatekeeper_check(
    target_bbox: tuple[int, int, int, int],
    screenshot_bytes: bytes,
) -> bool:
    """Quick visual check: is target region blank or has real content?

    Uses dual threshold: pixel variance (catches textured regions) and
    Canny edge density (catches colored buttons with clear borders).

    Returns True if region has visible content, False if blank.
    """
    if not _GATEKEEPER_ENABLED:
        return True  # Disabled → always pass

    try:
        import cv2
        import numpy as np

        x, y, w, h = target_bbox
        if w <= 0 or h <= 0:
            return False

        img = cv2.imdecode(
            np.frombuffer(screenshot_bytes, np.uint8), cv2.IMREAD_GRAYSCALE,
        )
        if img is None:
            return True  # Can't decode → don't block

        # Clamp to image bounds.
        y0 = max(0, y)
        y1 = min(img.shape[0], y + h)
        x0 = max(0, x)
        x1 = min(img.shape[1], x + w)
        if y1 <= y0 or x1 <= x0:
            return False

        crop = img[y0:y1, x0:x1]
        variance = float(np.var(crop))
        edges = cv2.Canny(crop, 50, 150)
        edge_density = float(np.count_nonzero(edges)) / max(crop.size, 1)

        return variance >= _VARIANCE_THRESHOLD or edge_density >= _EDGE_DENSITY_THRESHOLD
    except Exception:
        return True  # On error, don't block the action
