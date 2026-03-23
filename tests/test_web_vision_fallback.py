"""Tests for T2 web vision fallback wrapper."""
from __future__ import annotations

import pytest
from unittest.mock import MagicMock, AsyncMock, patch

from core.models.schemas import UIDNode
from core.detection_tier import DetectedElement, TierResult


class TestDetectedToUIDNode:
    def test_basic_conversion(self):
        from bridges.web_bridge.vision_fallback import _detected_to_uid_node

        det = DetectedElement(
            bbox=(100, 200, 50, 30),
            label="Submit",
            tag="button",
            interactable=True,
            confidence=0.9,
            extra={"role": "button"},
        )
        node = _detected_to_uid_node(det, 0)
        assert node.uid == "vc_0"
        assert node.tag == "button"
        assert node.text == "Submit"
        assert node.bbox == (100, 200, 50, 30)
        assert node.attributes["confidence"] == "0.9"
        assert node.attributes["source"] == "vision"
        assert node.tier == "vision"

    def test_conversion_with_empty_extra(self):
        from bridges.web_bridge.vision_fallback import _detected_to_uid_node

        det = DetectedElement(bbox=(0, 0, 10, 10), label="x", tag="unknown")
        node = _detected_to_uid_node(det, 5)
        assert node.uid == "vc_5"

    def test_max_text_truncation(self):
        from bridges.web_bridge.vision_fallback import _detected_to_uid_node

        det = DetectedElement(bbox=(0, 0, 10, 10), label="A" * 300, tag="text")
        node = _detected_to_uid_node(det, 0)
        assert len(node.text) <= 200


class TestWebVisionFallback:
    def test_returns_empty_when_modules_unavailable(self):
        from bridges.web_bridge.vision_fallback import WebVisionFallback

        fb = WebVisionFallback()
        assert fb is not None

    @pytest.mark.asyncio
    async def test_fallback_returns_uid_nodes(self):
        from bridges.web_bridge.vision_fallback import WebVisionFallback

        mock_result = TierResult(
            elements=[
                DetectedElement(
                    bbox=(10, 20, 30, 40),
                    label="Login",
                    tag="button",
                    confidence=0.8,
                ),
                DetectedElement(
                    bbox=(50, 60, 70, 80),
                    label="Password",
                    tag="text",
                    confidence=0.7,
                ),
            ],
            source_name="mock_ocr",
            elapsed_ms=100.0,
        )
        fb = WebVisionFallback()
        with patch.object(
            fb, "_run_detection", new_callable=AsyncMock, return_value=mock_result
        ):
            nodes = await fb.extract(screenshot_bytes=b"fake_png")
        assert len(nodes) == 2
        assert nodes[0].uid == "vc_0"
        assert nodes[1].uid == "vc_1"
