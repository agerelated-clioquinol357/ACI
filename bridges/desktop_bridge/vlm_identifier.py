"""
OpenClaw 2.0 ACI Framework - VLM Identifier (Last Resort).

For elements that remain unlabeled after cursor probe + OCR + contour +
tooltip detection.  Workflow:

    1. Check YAML knowledge base — if app+region_hash found, return cached label.
    2. If not cached: draw numbered red dots at unidentified centers on screenshot.
    3. Send annotated screenshot to external VLM API (GPT-4V / Claude Vision).
    4. Parse response → assign labels → cache to YAML knowledge base.

This tier should fire rarely: only for icon-only buttons with no tooltip.
After the first VLM identification the result is cached and reused forever.

Requires:
    - External VLM API key (OPENAI_API_KEY or ANTHROPIC_API_KEY)
    - Pillow for red dot drawing
    - YAML knowledge base module
"""

from __future__ import annotations

import base64
import io
import json
import logging
import os
from typing import Any, Optional

from core.detection_tier import DetectedElement, DetectionTier, TierResult

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependencies
# ---------------------------------------------------------------------------

try:
    from PIL import Image, ImageDraw, ImageFont
    _PIL_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore[assignment, misc]
    ImageDraw = None  # type: ignore[assignment]
    ImageFont = None  # type: ignore[assignment]
    _PIL_AVAILABLE = False

try:
    import httpx
    _HTTPX_AVAILABLE = True
except ImportError:
    httpx = None  # type: ignore[assignment]
    _HTTPX_AVAILABLE = False

# Knowledge base import (lazy — may not exist yet during initial setup).
_kb_module = None


def _get_kb():
    global _kb_module
    if _kb_module is None:
        try:
            from memory_core import knowledge_base as kb
            _kb_module = kb
        except ImportError:
            pass
    return _kb_module


# ---------------------------------------------------------------------------
# Configuration — hardcoded defaults, no external config file needed.
# Override via environment variables.
# ---------------------------------------------------------------------------

_DEFAULT_VLM_CONFIG = {
    "provider": os.environ.get("OPENCLAW_VLM_PROVIDER", "openai"),
    "model": os.environ.get("OPENCLAW_VLM_MODEL", "gpt-4o"),
    "api_key_env": os.environ.get("OPENCLAW_VLM_KEY_ENV", "OPENAI_API_KEY"),
    "max_dots_per_request": int(os.environ.get("OPENCLAW_VLM_MAX_DOTS", "20")),
    "enabled": os.environ.get("OPENCLAW_VLM_ENABLED", "true").lower() == "true",
}


def _load_vlm_config() -> dict:
    """Return VLM config (hardcoded defaults, env-var overridable)."""
    return dict(_DEFAULT_VLM_CONFIG)


# ---------------------------------------------------------------------------
# Red dot annotation
# ---------------------------------------------------------------------------

# Maximum image width before VLM API call (avoids input length limits).
_MAX_VLM_IMAGE_WIDTH = int(os.environ.get("OPENCLAW_VLM_MAX_WIDTH", "1280"))
# Maximum base64 payload size in bytes (Qwen/Dashscope limit ~983KB).
_MAX_VLM_PAYLOAD_BYTES = int(os.environ.get("OPENCLAW_VLM_MAX_PAYLOAD", "900000"))


def _downscale_for_vlm(img: "Image.Image") -> bytes:
    """Downscale and JPEG-compress an image to fit VLM API size limits.

    Returns JPEG bytes that are guaranteed to be under _MAX_VLM_PAYLOAD_BYTES
    when base64-encoded (base64 expands ~33%, so raw limit is ~675KB).
    """
    # Downscale if wider than max.
    if img.width > _MAX_VLM_IMAGE_WIDTH:
        ratio = _MAX_VLM_IMAGE_WIDTH / img.width
        new_h = max(1, int(img.height * ratio))
        img = img.resize((_MAX_VLM_IMAGE_WIDTH, new_h), Image.LANCZOS)

    # Try JPEG at decreasing quality until size fits.
    for quality in (85, 70, 55, 40):
        buf = io.BytesIO()
        img.save(buf, format="JPEG", quality=quality)
        raw_bytes = buf.getvalue()
        # base64 inflates size by ~4/3
        if len(raw_bytes) * 4 // 3 <= _MAX_VLM_PAYLOAD_BYTES:
            return raw_bytes

    # Last resort: further shrink to half width.
    new_w = max(1, img.width // 2)
    new_h = max(1, img.height // 2)
    img = img.resize((new_w, new_h), Image.LANCZOS)
    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=40)
    return buf.getvalue()


def draw_red_dots(
    screenshot_bytes: bytes,
    elements: list[tuple[int, DetectedElement]],  # (dot_number, element)
) -> bytes:
    """Draw numbered red dots at element centers on a screenshot.

    Returns JPEG bytes, downscaled to fit VLM API size limits.
    """
    if not _PIL_AVAILABLE:
        return screenshot_bytes

    try:
        img = Image.open(io.BytesIO(screenshot_bytes)).convert("RGB")
        draw = ImageDraw.Draw(img)

        try:
            font = ImageFont.truetype("arial.ttf", 16)
        except (OSError, IOError):
            try:
                font = ImageFont.truetype("DejaVuSans.ttf", 16)
            except (OSError, IOError):
                font = ImageFont.load_default()

        for dot_num, elem in elements:
            cx = elem.bbox[0] + elem.bbox[2] // 2
            cy = elem.bbox[1] + elem.bbox[3] // 2
            radius = 12

            # Red filled circle.
            draw.ellipse(
                [cx - radius, cy - radius, cx + radius, cy + radius],
                fill=(255, 0, 0),
                outline=(255, 255, 255),
                width=2,
            )

            # White number in center.
            text = str(dot_num)
            text_bbox = draw.textbbox((0, 0), text, font=font)
            tw = text_bbox[2] - text_bbox[0]
            th = text_bbox[3] - text_bbox[1]
            draw.text(
                (cx - tw // 2, cy - th // 2),
                text,
                fill=(255, 255, 255),
                font=font,
            )

        # Downscale + JPEG compress to fit VLM API limits.
        return _downscale_for_vlm(img)

    except Exception as exc:
        logger.error("vlm_identifier: draw_red_dots failed: %s", exc)
        return screenshot_bytes


# ---------------------------------------------------------------------------
# VLM API call
# ---------------------------------------------------------------------------

_VLM_PROMPT = """Identify the UI elements at each numbered red dot in this screenshot.
Return ONLY a JSON array, no other text. Format:
[{"dot": 1, "label": "element name", "type": "button/icon/link/menu/other"}]

Be specific with labels (e.g. "close button", "settings gear icon", "hamburger menu").
"""


def _call_openai_vlm(image_b64: str, config: dict) -> Optional[list[dict]]:
    """Call OpenAI/GPT-4V API with annotated screenshot."""
    api_key = os.environ.get(config.get("api_key_env", "OPENAI_API_KEY"), "")
    if not api_key:
        logger.warning("vlm_identifier: No API key found for OpenAI VLM.")
        return None

    model = config.get("model", "gpt-4o")

    try:
        response = httpx.post(
            "https://api.openai.com/v1/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": _VLM_PROMPT},
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:image/jpeg;base64,{image_b64}",
                                    "detail": "high",
                                },
                            },
                        ],
                    }
                ],
                "max_tokens": 1000,
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        text = data["choices"][0]["message"]["content"]

        # Extract JSON from response (may be wrapped in markdown code block).
        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.startswith("json"):
                text = text[4:].strip()

        return json.loads(text)

    except Exception as exc:
        logger.error("vlm_identifier: OpenAI API call failed: %s", exc)
        return None


def _call_anthropic_vlm(image_b64: str, config: dict) -> Optional[list[dict]]:
    """Call Anthropic Claude Vision API with annotated screenshot."""
    api_key = os.environ.get(config.get("api_key_env", "ANTHROPIC_API_KEY"), "")
    if not api_key:
        logger.warning("vlm_identifier: No API key found for Anthropic VLM.")
        return None

    model = config.get("model", "claude-sonnet-4-20250514")

    try:
        response = httpx.post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
                "Content-Type": "application/json",
            },
            json={
                "model": model,
                "max_tokens": 1000,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "image",
                                "source": {
                                    "type": "base64",
                                    "media_type": "image/jpeg",
                                    "data": image_b64,
                                },
                            },
                            {"type": "text", "text": _VLM_PROMPT},
                        ],
                    }
                ],
            },
            timeout=30.0,
        )
        response.raise_for_status()
        data = response.json()
        text = data["content"][0]["text"]

        text = text.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
            if text.endswith("```"):
                text = text[:-3]
            text = text.strip()
            if text.startswith("json"):
                text = text[4:].strip()

        return json.loads(text)

    except Exception as exc:
        logger.error("vlm_identifier: Anthropic API call failed: %s", exc)
        return None


# ---------------------------------------------------------------------------
# Detection Tier
# ---------------------------------------------------------------------------

class VLMIdentifier(DetectionTier):
    """Red dot annotation → external VLM — last resort for unlabeled elements."""

    def __init__(self, *, max_dots: int = 20) -> None:
        self._max_dots = max_dots

    @property
    def name(self) -> str:
        return "vlm"

    @property
    def priority(self) -> float:
        return 4.0

    def is_available(self) -> bool:
        return _PIL_AVAILABLE and _HTTPX_AVAILABLE

    def detect(
        self,
        screenshot_bytes: bytes,
        existing_elements: list[DetectedElement],
        *,
        roi: Optional[tuple[int, int, int, int]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> TierResult:
        config = _load_vlm_config()
        if not config.get("enabled", True):
            return TierResult(elements=[], source_name=self.name)

        # Find unlabeled interactable elements.
        unlabeled = [
            (i, e) for i, e in enumerate(existing_elements)
            if e.interactable and not e.label
        ]

        if not unlabeled:
            return TierResult(elements=[], source_name=self.name)

        app_name = (context or {}).get("app_name", "unknown")
        kb = _get_kb()

        # Stage 1: Check knowledge base cache.
        still_unlabeled: list[tuple[int, DetectedElement]] = []
        for idx, elem in unlabeled:
            if kb:
                window_size = (context or {}).get("window_size", (1920, 1080))
                rh = kb.region_hash(elem.bbox, window_size)
                cached_label = kb.lookup(app_name, rh)
                if cached_label:
                    elem.label = cached_label
                    elem.confidence = max(elem.confidence, 0.9)
                    logger.info(
                        "vlm_identifier: cache hit for %s: %r",
                        rh[:8], cached_label,
                    )
                    continue
            still_unlabeled.append((idx, elem))

        if not still_unlabeled:
            return TierResult(elements=[], source_name=self.name)

        if not screenshot_bytes:
            return TierResult(elements=[], source_name=self.name)

        # Stage 2: Red dot annotation + VLM call.
        # Limit number of dots per request.
        batch = still_unlabeled[:self._max_dots]

        dot_elements = [(i + 1, elem) for i, (_, elem) in enumerate(batch)]
        annotated = draw_red_dots(screenshot_bytes, dot_elements)

        image_b64 = base64.b64encode(annotated).decode("ascii")

        # Call VLM API.
        provider = config.get("provider", "openai")
        results: Optional[list[dict]] = None

        if provider == "openai":
            results = _call_openai_vlm(image_b64, config)
        elif provider == "anthropic":
            results = _call_anthropic_vlm(image_b64, config)
        else:
            # Try OpenAI as default.
            results = _call_openai_vlm(image_b64, config)

        if not results:
            logger.warning("vlm_identifier: VLM returned no results.")
            return TierResult(elements=[], source_name=self.name)

        # Stage 3: Assign labels + cache to YAML.
        for vlm_item in results:
            dot_num = vlm_item.get("dot", 0)
            label = vlm_item.get("label", "")
            elem_type = vlm_item.get("type", "button")

            if not label or dot_num < 1 or dot_num > len(batch):
                continue

            _, elem = batch[dot_num - 1]
            elem.label = label
            elem.tag = elem_type
            elem.confidence = max(elem.confidence, 0.7)

            # Cache to knowledge base.
            if kb:
                window_size = (context or {}).get("window_size", (1920, 1080))
                rh = kb.region_hash(elem.bbox, window_size)
                try:
                    # Crop icon region for template matching.
                    crop_bytes = None
                    if _PIL_AVAILABLE:
                        img = Image.open(io.BytesIO(screenshot_bytes))
                        x, y, w, h = elem.bbox
                        crop = img.crop((x, y, x + w, y + h))
                        cbuf = io.BytesIO()
                        crop.save(cbuf, format="PNG")
                        crop_bytes = cbuf.getvalue()

                    kb.save_element(app_name, rh, label, crop_bytes)
                    logger.info(
                        "vlm_identifier: cached %r → %r for app=%s",
                        rh[:8], label, app_name,
                    )
                except Exception as exc:
                    logger.warning("vlm_identifier: failed to cache: %s", exc)

        return TierResult(elements=[], source_name=self.name)
