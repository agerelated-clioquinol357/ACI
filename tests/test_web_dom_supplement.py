"""Tests for T1 DOM supplement JS module."""
from __future__ import annotations
import pytest

class TestDomSupplementJS:
    def test_js_file_exists_and_parseable(self):
        from pathlib import Path
        js_path = Path(__file__).parent.parent / "bridges" / "web_bridge" / "dom_supplement.js"
        source = js_path.read_text(encoding="utf-8")
        assert "OpenClawSupplement" in source
        assert "extractSupplement" in source

    def test_js_has_viewport_filtering(self):
        from pathlib import Path
        js_path = Path(__file__).parent.parent / "bridges" / "web_bridge" / "dom_supplement.js"
        source = js_path.read_text(encoding="utf-8")
        assert "innerWidth" in source or "clientWidth" in source
        assert "getBoundingClientRect" in source

    def test_js_has_max_elements_cap(self):
        from pathlib import Path
        js_path = Path(__file__).parent.parent / "bridges" / "web_bridge" / "dom_supplement.js"
        source = js_path.read_text(encoding="utf-8")
        assert "MAX_SUPPLEMENT" in source
