"""
OpenClaw 2.0 ACI Framework - Contour Detection Tier (OCR-sparse areas only).

Only processes regions flagged ``needs_contour`` by the OCR tier — these are
areas where cursor probing detected interactable points but OCR found no text
(typically icon buttons).

Algorithm per region:
    1. Crop + pad → grayscale → Canny edge → morphological close
    2. findContours(RETR_EXTERNAL) → filter by min area / aspect ratio
    3. NMS (IoU threshold) → deduplicate overlapping boxes
    4. Cross-reference cursor probe: if contour contains HAND/IBEAM → interactable

Typical timing: ~25ms (small regions only, not full screen).
"""

from __future__ import annotations

import io
import logging
from typing import Any, Optional

from core.detection_tier import DetectedElement, DetectionTier, TierResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    import cv2
    import numpy as np
    _CV2_AVAILABLE = True
except ImportError:
    cv2 = None  # type: ignore[assignment]
    np = None  # type: ignore[assignment]
    _CV2_AVAILABLE = False
    logger.info(
        "contour_detector: OpenCV not available. Contour detection disabled. "
        "Install with: pip install opencv-python-headless numpy"
    )

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore[assignment, misc]
    _PIL_AVAILABLE = False


# ---------------------------------------------------------------------------
# NMS
# ---------------------------------------------------------------------------

def _nms(boxes: list[tuple[int, int, int, int]], iou_threshold: float = 0.3) -> list[tuple[int, int, int, int]]:
    """Non-maximum suppression on (x, y, w, h) boxes by area (largest first)."""
    if not boxes:
        return []

    # Sort by area descending.
    sorted_boxes = sorted(boxes, key=lambda b: b[2] * b[3], reverse=True)
    keep: list[tuple[int, int, int, int]] = []

    for box in sorted_boxes:
        suppress = False
        for kept in keep:
            iou = _iou(box, kept)
            if iou > iou_threshold:
                suppress = True
                break
        if not suppress:
            keep.append(box)

    return keep


def _iou(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Intersection over Union for (x, y, w, h) boxes."""
    ax1, ay1 = a[0], a[1]
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx1, by1 = b[0], b[1]
    bx2, by2 = b[0] + b[2], b[1] + b[3]

    ix1, iy1 = max(ax1, bx1), max(ay1, by1)
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)

    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0

    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


# ---------------------------------------------------------------------------
# Contour extraction for a single region
# ---------------------------------------------------------------------------

def _find_contours_in_region(
    image_arr: "np.ndarray",
    region: tuple[int, int, int, int],
    *,
    pad: int = 10,
    min_area: int = 64,
    nms_iou: float = 0.3,
) -> list[tuple[int, int, int, int]]:
    """Find contour bounding boxes within a padded region of the image.

    Args:
        image_arr: Full image as numpy array (BGR).
        region: (x, y, w, h) region to analyze.
        pad: Pixel padding around region.
        min_area: Minimum contour area to keep.
        nms_iou: NMS IoU threshold.

    Returns:
        List of (x, y, w, h) bounding boxes in full-image coordinates.
    """
    h_img, w_img = image_arr.shape[:2]
    rx, ry, rw, rh = region

    # Padded crop bounds.
    x1 = max(0, rx - pad)
    y1 = max(0, ry - pad)
    x2 = min(w_img, rx + rw + pad)
    y2 = min(h_img, ry + rh + pad)

    crop = image_arr[y1:y2, x1:x2]
    if crop.size == 0:
        return []

    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)

    # Canny edge detection.
    edges = cv2.Canny(gray, 50, 150)

    # Morphological close to connect nearby edges.
    kernel = cv2.getStructuringElement(cv2.MORPH_RECT, (5, 5))
    closed = cv2.morphologyEx(edges, cv2.MORPH_CLOSE, kernel)

    contours, _ = cv2.findContours(closed, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

    boxes: list[tuple[int, int, int, int]] = []
    for contour in contours:
        area = cv2.contourArea(contour)
        if area < min_area:
            continue

        bx, by, bw, bh = cv2.boundingRect(contour)

        # Filter extreme aspect ratios (likely noise).
        aspect = max(bw, bh) / max(min(bw, bh), 1)
        if aspect > 10:
            continue

        # Convert back to full-image coordinates.
        boxes.append((bx + x1, by + y1, bw, bh))

    return _nms(boxes, nms_iou)


# ---------------------------------------------------------------------------
# Detection Tier
# ---------------------------------------------------------------------------

class ContourDetector(DetectionTier):
    """OpenCV contour detection — only in OCR-sparse areas."""

    def __init__(
        self,
        *,
        min_area: int = 36,
        nms_iou_threshold: float = 0.3,
        merge_gap_px: int = 5,
    ) -> None:
        self._min_area = min_area
        self._nms_iou = nms_iou_threshold
        self._merge_gap = merge_gap_px

    @property
    def name(self) -> str:
        return "contour"

    @property
    def priority(self) -> float:
        return 3.0

    def is_available(self) -> bool:
        return _CV2_AVAILABLE

    def detect(
        self,
        screenshot_bytes: bytes,
        existing_elements: list[DetectedElement],
        *,
        roi: Optional[tuple[int, int, int, int]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> TierResult:
        if not _CV2_AVAILABLE:
            return TierResult(elements=[], source_name=self.name)

        # Process regions flagged needs_contour OR unlabeled interactable elements.
        sparse_regions = [
            e for e in existing_elements
            if e.needs_contour or (e.interactable and not e.label)
        ]
        if not sparse_regions:
            return TierResult(elements=[], source_name=self.name)

        # Decode screenshot.
        try:
            arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
            image = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if image is None:
                return TierResult(elements=[], source_name=self.name)
        except Exception as exc:
            logger.warning("contour_detector: Failed to decode screenshot: %s", exc)
            return TierResult(elements=[], source_name=self.name)

        new_elements: list[DetectedElement] = []

        for sparse_elem in sparse_regions:
            contour_boxes = _find_contours_in_region(
                image,
                sparse_elem.bbox,
                min_area=self._min_area,
                nms_iou=self._nms_iou,
            )

            for cbox in contour_boxes:
                new_elements.append(DetectedElement(
                    bbox=cbox,
                    label="",  # unlabeled — may need VLM
                    tag=sparse_elem.tag,  # inherit from cursor probe
                    interactable=sparse_elem.interactable,
                    confidence=0.4,
                    cursor_type=sparse_elem.cursor_type,
                ))

            # If contour found tighter boxes, update the original sparse element.
            if contour_boxes and len(contour_boxes) == 1:
                sparse_elem.bbox = contour_boxes[0]
                sparse_elem.confidence = max(sparse_elem.confidence, 0.5)

        return TierResult(elements=new_elements, source_name=self.name)


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _test() -> None:
    """Quick test: capture screenshot, detect contours in a specified region."""
    import time

    if not _CV2_AVAILABLE or not _PIL_AVAILABLE:
        print("OpenCV and/or PIL not available.")
        return

    from PIL import ImageGrab

    print("Capturing screenshot...")
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    screenshot = buf.getvalue()

    print(f"Screenshot: {img.width}x{img.height}")

    # Test on center region.
    cx, cy = img.width // 4, img.height // 4
    test_region = (cx, cy, img.width // 2, img.height // 2)

    arr = np.frombuffer(screenshot, dtype=np.uint8)
    image = cv2.imdecode(arr, cv2.IMREAD_COLOR)

    t0 = time.perf_counter()
    boxes = _find_contours_in_region(image, test_region)
    elapsed = (time.perf_counter() - t0) * 1000

    print(f"\nFound {len(boxes)} contours in center region in {elapsed:.1f}ms:")
    for i, box in enumerate(boxes[:20]):
        print(f"  [{i}] bbox={box}")


if __name__ == "__main__":
    _test()
