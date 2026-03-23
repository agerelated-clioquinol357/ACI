"""Tests for T0 CDP accessibility tree extraction."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock
from core.models.schemas import UIDNode


# --- Mock CDP responses ---

_MOCK_AX_TREE = {
    "nodes": [
        {"nodeId": "1", "role": {"value": "WebArea"}, "name": {"value": "Test Page"}, "backendDOMNodeId": 1},
        {"nodeId": "2", "role": {"value": "button"}, "name": {"value": "Submit"}, "backendDOMNodeId": 10, "properties": []},
        {"nodeId": "3", "role": {"value": "link"}, "name": {"value": "Home"}, "backendDOMNodeId": 11, "properties": []},
        {"nodeId": "4", "role": {"value": "textbox"}, "name": {"value": "Email"}, "backendDOMNodeId": 12, "properties": []},
        {"nodeId": "5", "role": {"value": "heading"}, "name": {"value": "Welcome"}, "backendDOMNodeId": 13, "properties": [{"name": "level", "value": {"value": 1}}]},
        {"nodeId": "6", "role": {"value": "generic"}, "name": {"value": ""}, "backendDOMNodeId": 14, "properties": []},
    ]
}

def _make_box_model(x, y, w, h):
    return {"model": {"content": [x, y, x+w, y, x+w, y+h, x, y+h]}}


class TestA11yExtractor:
    def test_filter_interactive_roles(self):
        from bridges.web_bridge.a11y_extractor import A11yExtractor, _INTERACTIVE_ROLES
        assert "button" in _INTERACTIVE_ROLES
        assert "link" in _INTERACTIVE_ROLES
        assert "textbox" in _INTERACTIVE_ROLES
        assert "heading" in _INTERACTIVE_ROLES
        assert "generic" not in _INTERACTIVE_ROLES
        assert "WebArea" not in _INTERACTIVE_ROLES

    def test_parse_box_model_to_bbox(self):
        from bridges.web_bridge.a11y_extractor import _quad_to_bbox
        quad = [100, 200, 250, 200, 250, 240, 100, 240]
        assert _quad_to_bbox(quad) == (100, 200, 150, 40)

    def test_viewport_filter(self):
        from bridges.web_bridge.a11y_extractor import _is_in_viewport
        vw, vh = 1280, 900
        assert _is_in_viewport((100, 100, 50, 30), vw, vh) is True
        assert _is_in_viewport((-200, 100, 50, 30), vw, vh) is False
        assert _is_in_viewport((100, 1100, 50, 30), vw, vh) is False
        assert _is_in_viewport((-90, 100, 50, 30), vw, vh) is True

    @pytest.mark.asyncio
    async def test_extract_filters_and_resolves_bboxes(self):
        from bridges.web_bridge.a11y_extractor import A11yExtractor
        cdp = AsyncMock()
        cdp.send = AsyncMock(side_effect=lambda method, params=None: {
            "Accessibility.getFullAXTree": _MOCK_AX_TREE,
        }.get(method, _make_box_model(100, 100, 80, 30)))
        extractor = A11yExtractor(viewport_width=1280, viewport_height=900)
        nodes = await extractor.extract(cdp)
        assert len(nodes) == 4
        roles = {n.role for n in nodes}
        assert "button" in roles
        assert "link" in roles
        assert "heading" in roles
        assert "generic" not in roles

    @pytest.mark.asyncio
    async def test_getboxmodel_failure_skips_node(self):
        from bridges.web_bridge.a11y_extractor import A11yExtractor
        call_count = 0
        async def mock_send(method, params=None):
            nonlocal call_count
            if method == "Accessibility.getFullAXTree":
                return _MOCK_AX_TREE
            call_count += 1
            if call_count == 1:
                raise Exception("Node is not visible")
            return _make_box_model(100, 100, 80, 30)
        cdp = AsyncMock()
        cdp.send = AsyncMock(side_effect=mock_send)
        extractor = A11yExtractor(viewport_width=1280, viewport_height=900)
        nodes = await extractor.extract(cdp)
        assert len(nodes) == 3

    def test_smart_prioritization_caps_at_max(self):
        from bridges.web_bridge.a11y_extractor import _prioritize
        nodes = []
        for i in range(400):
            role = "button" if i < 200 else ("heading" if i < 250 else "text")
            nodes.append(UIDNode(
                uid=f"@e{i}", tag="div", text=f"el{i}", role=role,
                bbox=(i, 0, 10, 10), interactable=True,
            ))
        result = _prioritize(nodes, max_count=300)
        assert len(result) == 300
        button_count = sum(1 for n in result if n.role == "button")
        assert button_count == 200
