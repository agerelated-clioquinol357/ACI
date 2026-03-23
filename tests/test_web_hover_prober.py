"""Tests for proactive hover probing."""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.models.schemas import UIDNode


class TestHoverProber:
    def test_trigger_detection_aria_haspopup(self):
        from bridges.web_bridge.hover_prober import _is_trigger
        node = UIDNode(uid="@e1", tag="button", text="Menu", bbox=(10, 10, 50, 30),
                       attributes={"haspopup": "true"}, interactable=True)
        assert _is_trigger(node) is True

    def test_trigger_detection_aria_expanded_false(self):
        from bridges.web_bridge.hover_prober import _is_trigger
        node = UIDNode(uid="@e2", tag="button", text="More", bbox=(10, 10, 50, 30),
                       attributes={"expanded": "false"}, interactable=True)
        assert _is_trigger(node) is True

    def test_non_trigger_ignored(self):
        from bridges.web_bridge.hover_prober import _is_trigger
        node = UIDNode(uid="@e3", tag="button", text="Submit", bbox=(10, 10, 50, 30),
                       attributes={}, interactable=True)
        assert _is_trigger(node) is False

    @pytest.mark.asyncio
    async def test_probe_budget_respected(self):
        from bridges.web_bridge.hover_prober import HoverProber
        prober = HoverProber(max_probes=2, max_elements=10)
        triggers = [
            UIDNode(uid=f"@e{i}", tag="button", text=f"Trigger{i}",
                    bbox=(i*100, 10, 50, 30), attributes={"haspopup": "true"}, interactable=True)
            for i in range(5)
        ]
        page = AsyncMock()
        page.mouse = AsyncMock()
        page.viewport_size = {"width": 1280, "height": 900}
        extract_fn = AsyncMock(return_value=[])
        result = await prober.probe(page, triggers, extract_fn)
        assert page.mouse.move.call_count <= 4  # 2 hovers + 2 resets max
