"""Integration smoke tests for the redesigned web bridge."""
from __future__ import annotations

import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.models.schemas import UIDNode


class TestWebBridgeIntegration:

    def test_all_new_modules_importable(self):
        """All new modules import without error."""
        from bridges.web_bridge.a11y_extractor import A11yExtractor
        from bridges.web_bridge.vision_fallback import WebVisionFallback
        from bridges.web_bridge.hover_prober import HoverProber
        from bridges.web_bridge.frame_manager import FrameManager, RefCounter
        assert A11yExtractor is not None
        assert WebVisionFallback is not None
        assert HoverProber is not None
        assert FrameManager is not None
        assert RefCounter is not None

    def test_executor_has_new_modules(self):
        """WebExecutor wires up all new modules."""
        from bridges.web_bridge.executor import WebExecutor
        page = MagicMock()
        executor = WebExecutor(page, "test")
        assert hasattr(executor, "_vision_fallback")
        assert hasattr(executor, "_hover_prober")
        assert hasattr(executor, "_frame_manager")
        assert hasattr(executor, "_ref_counter")
        assert hasattr(executor, "rollback")

    def test_legacy_dom_parser_still_exists(self):
        """dom_parser.js is preserved as legacy fallback."""
        from pathlib import Path
        legacy = Path(__file__).parent.parent / "bridges" / "web_bridge" / "dom_parser.js"
        assert legacy.exists()
        source = legacy.read_text(encoding="utf-8")
        assert "OpenClawExtractor" in source

    def test_ref_counter_integrated_in_executor(self):
        """Executor uses RefCounter for ref assignment."""
        from bridges.web_bridge.executor import WebExecutor
        page = MagicMock()
        executor = WebExecutor(page, "test")
        assert hasattr(executor, "_ref_counter")

    def test_env_vars_documented(self):
        """All env vars from the spec exist in at least one module."""
        import bridges.web_bridge.hover_prober as hp
        import bridges.web_bridge.vision_fallback as vf
        import bridges.web_bridge.a11y_extractor as ae
        source_hp = open(hp.__file__, encoding="utf-8").read()
        source_vf = open(vf.__file__, encoding="utf-8").read()
        source_ae = open(ae.__file__, encoding="utf-8").read()
        assert "OPENCLAW_WEB_HOVER_PROBE" in source_hp
        assert "OPENCLAW_WEB_VISION_FALLBACK" in source_vf
        assert "OPENCLAW_WEB_T0_MAX" in source_ae
