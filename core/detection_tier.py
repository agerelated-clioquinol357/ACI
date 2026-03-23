"""
OpenClaw 2.0 ACI Framework - Pluggable Detection Tier Protocol.

Defines the abstract base class for element detection tiers and a registry
that runs them in a waterfall (lowest priority number first).  Each tier
receives the elements found by all preceding tiers so it can fuse or skip
redundant work.

Tiers:
    1. CursorProbe  (priority 1.0) — Win32 cursor mutation scan
    2. FastOCR      (priority 2.0) — Windows OCR API / Tesseract
    3. ContourDetector (priority 3.0) — OpenCV contour in sparse areas
    4. VLMIdentifier  (priority 4.0) — Red-dot annotation → external VLM
"""

from __future__ import annotations

import logging
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Data contracts
# ---------------------------------------------------------------------------

@dataclass
class DetectedElement:
    """A single element found by a detection tier."""

    bbox: tuple[int, int, int, int]  # (x, y, w, h) in screen pixels
    label: str = ""
    tag: str = "unknown"
    interactable: bool = True
    confidence: float = 0.5
    cursor_type: Optional[str] = None
    needs_contour: bool = False  # flagged by OCR tier for contour fallback
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class TierResult:
    """Output of a single detection tier run."""

    elements: list[DetectedElement]
    source_name: str
    elapsed_ms: float = 0.0


# ---------------------------------------------------------------------------
# Abstract base
# ---------------------------------------------------------------------------

class DetectionTier(ABC):
    """Base class that every detection tier must implement."""

    @property
    @abstractmethod
    def name(self) -> str:
        """Short identifier for this tier (e.g. 'cursor_probe')."""

    @property
    @abstractmethod
    def priority(self) -> float:
        """Lower values run first in the waterfall."""

    @abstractmethod
    def detect(
        self,
        screenshot_bytes: bytes,
        existing_elements: list[DetectedElement],
        *,
        roi: Optional[tuple[int, int, int, int]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> TierResult:
        """Run detection on a screenshot.

        Args:
            screenshot_bytes: Full screenshot as PNG bytes.
            existing_elements: Elements found by all preceding tiers.
            roi: Optional region of interest ``(x, y, w, h)``.
            context: Arbitrary context dict (app name, HWND, etc.).

        Returns:
            TierResult with the elements this tier found.
        """

    def is_available(self) -> bool:
        """Return True if this tier's dependencies are satisfied."""
        return True


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

class TierRegistry:
    """Ordered collection of detection tiers with waterfall execution."""

    def __init__(self) -> None:
        self._tiers: list[DetectionTier] = []

    def register(self, tier: DetectionTier) -> None:
        """Add a tier and keep the list sorted by priority."""
        if not tier.is_available():
            logger.warning(
                "detection_tier: tier %r not available, skipping registration.",
                tier.name,
            )
            return
        self._tiers.append(tier)
        self._tiers.sort(key=lambda t: t.priority)
        logger.info(
            "detection_tier: registered tier %r (priority=%.1f).",
            tier.name,
            tier.priority,
        )

    def run_waterfall(
        self,
        screenshot_bytes: bytes,
        *,
        roi: Optional[tuple[int, int, int, int]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> list[TierResult]:
        """Execute all tiers in priority order, passing accumulated elements.

        Returns:
            List of TierResult objects (one per tier that ran).
        """
        all_results: list[TierResult] = []
        accumulated: list[DetectedElement] = []

        for tier in self._tiers:
            try:
                t0 = time.perf_counter()
                result = tier.detect(
                    screenshot_bytes,
                    list(accumulated),  # copy so tiers can't mutate upstream
                    roi=roi,
                    context=context,
                )
                result.elapsed_ms = (time.perf_counter() - t0) * 1000
                result.source_name = tier.name

                all_results.append(result)
                accumulated.extend(result.elements)

                logger.info(
                    "detection_tier: %s found %d elements in %.1fms.",
                    tier.name,
                    len(result.elements),
                    result.elapsed_ms,
                )
            except Exception as exc:
                logger.error(
                    "detection_tier: tier %r failed: %s", tier.name, exc,
                )

        return all_results

    @property
    def tier_names(self) -> list[str]:
        """Names of all registered tiers in priority order."""
        return [t.name for t in self._tiers]
