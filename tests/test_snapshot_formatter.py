"""Tests for snapshot formatter."""
from __future__ import annotations

from core.models.schemas import UIDNode


class TestSnapshotFormatter:

    def test_basic_format(self):
        from bridges.web_bridge.snapshot_formatter import format_snapshot
        elements = [
            UIDNode(uid="@e1", tag="button", text="Search", role="button",
                    bbox=(100, 200, 80, 30), interactable=True),
            UIDNode(uid="@e2", tag="a", text="Home", role="link",
                    bbox=(10, 10, 50, 20), interactable=True,
                    attributes={"href": "https://example.com"}),
            UIDNode(uid="@e3", tag="input", text="", role="textbox",
                    bbox=(200, 100, 300, 30), interactable=True,
                    attributes={"type": "text"}),
        ]
        result = format_snapshot(elements)
        assert '[@e1]' in result
        assert '[@e2]' in result
        assert '[@e3]' in result
        assert 'button' in result
        assert '"Search"' in result
        assert 'link' in result

    def test_empty_elements(self):
        from bridges.web_bridge.snapshot_formatter import format_snapshot
        result = format_snapshot([])
        assert "empty" in result.lower() or "no" in result.lower()

    def test_vision_element_tagged(self):
        from bridges.web_bridge.snapshot_formatter import format_snapshot
        elements = [
            UIDNode(uid="@e5", tag="button", text="Play", role="button",
                    bbox=(400, 300, 100, 50), interactable=True, tier="vision"),
        ]
        result = format_snapshot(elements)
        assert "vision-detected" in result

    def test_long_text_truncated(self):
        from bridges.web_bridge.snapshot_formatter import format_snapshot
        elements = [
            UIDNode(uid="@e1", tag="div", text="A" * 100, role="button",
                    bbox=(0, 0, 10, 10), interactable=True),
        ]
        result = format_snapshot(elements)
        assert "..." in result
        assert len(result) < 200

    def test_page_summary_includes_header(self):
        from bridges.web_bridge.snapshot_formatter import format_page_summary
        elements = [
            UIDNode(uid="@e1", tag="button", text="OK", role="button",
                    bbox=(0, 0, 10, 10), interactable=True),
        ]
        result = format_page_summary("https://example.com", "Example", elements)
        assert "Page: Example" in result
        assert "URL: https://example.com" in result
        assert "1 interactive" in result
        assert '[@e1]' in result

    def test_non_interactive_in_landmarks(self):
        from bridges.web_bridge.snapshot_formatter import format_page_summary
        elements = [
            UIDNode(uid="@e1", tag="button", text="Click", role="button",
                    bbox=(0, 0, 10, 10), interactable=True),
            UIDNode(uid="@e2", tag="heading", text="Title", role="heading",
                    bbox=(0, 0, 100, 30), interactable=False),
        ]
        result = format_page_summary("https://x.com", "X", elements)
        assert "Landmarks:" in result
        assert "heading" in result
