"""Integration tests for the redesigned WebExecutor tiered pipeline."""
from __future__ import annotations
import pytest
from unittest.mock import AsyncMock, MagicMock, patch
from core.models.schemas import UIDNode, ActionRequest


class TestTieredPerceive:
    @pytest.mark.asyncio
    async def test_perceive_uses_t0_and_t1_parallel(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.viewport_size = {"width": 1280, "height": 900}
        page.evaluate = AsyncMock(return_value=[])
        page.screenshot = AsyncMock(return_value=b"png")
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="")
        cdp = AsyncMock()
        with patch("bridges.web_bridge.executor.WebExecutor._get_cdp_session", return_value=cdp):
            executor = WebExecutor(page, "test-session")
            t0_nodes = [UIDNode(uid="_pending", tag="button", text="Submit", bbox=(100, 100, 80, 30), interactable=True, tier="a11y")]
            t1_nodes = [UIDNode(uid="_pending", tag="div", text="Custom", bbox=(300, 100, 60, 30), interactable=True, tier="dom")]
            with patch.object(executor, "_run_t0", return_value=t0_nodes), \
                 patch.object(executor, "_run_t1", return_value=t1_nodes):
                perception = await executor.perceive()
            assert len(perception.elements) == 2
            assert perception.elements[0].uid == "@e1"
            assert perception.elements[1].uid == "@e2"

    @pytest.mark.asyncio
    async def test_perceive_falls_back_to_legacy_on_cdp_failure(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.viewport_size = {"width": 1280, "height": 900}
        page.evaluate = AsyncMock(return_value=[
            {"ref": "@e1", "uid": "oc_0", "tag": "button", "text": "OK", "attrs": {}, "bbox": [10, 10, 50, 30]},
        ])
        with patch("bridges.web_bridge.executor.WebExecutor._get_cdp_session", side_effect=Exception("CDP not available")):
            executor = WebExecutor(page, "test-session")
            perception = await executor.perceive()
        assert len(perception.elements) >= 1
        assert perception.elements[0].tag == "button"

    def test_rollback_stub_exists(self):
        from bridges.web_bridge.executor import WebExecutor
        page = MagicMock()
        executor = WebExecutor(page, "test")
        assert hasattr(executor, "rollback")

    @pytest.mark.asyncio
    async def test_frame_aware_click(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.mouse = AsyncMock()
        executor = WebExecutor(page, "test")
        executor._ref_counter._ref_to_bbox["@e5"] = (100, 200, 50, 30)
        executor._ref_counter._ref_to_frame["@e5"] = "main"
        action = ActionRequest(session_id="test", action_type="click", target_uid="@e5")
        result = await executor.act(action)
        assert result.success is True

    @pytest.mark.asyncio
    async def test_scroll_settle_not_500ms(self):
        import time
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.mouse = AsyncMock()
        page.evaluate = AsyncMock(return_value=True)
        executor = WebExecutor(page, "test")
        start = time.monotonic()
        action = ActionRequest(session_id="test", action_type="scroll", value="down")
        await executor.act(action)
        elapsed = time.monotonic() - start
        assert elapsed < 0.4


class TestPageTextExtraction:
    @pytest.mark.asyncio
    async def test_perceive_includes_page_text(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.viewport_size = {"width": 1280, "height": 900}
        page.evaluate = AsyncMock(return_value=[])
        page.screenshot = AsyncMock(return_value=b"png")
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)
        # inner_text returns readable page content
        page.inner_text = AsyncMock(return_value="Hello World\nThis is the article body text about testing.")
        cdp = AsyncMock()
        with patch("bridges.web_bridge.executor.WebExecutor._get_cdp_session", return_value=cdp):
            executor = WebExecutor(page, "test-session")
            t0_nodes = [UIDNode(uid="_pending", tag="button", text="OK", bbox=(10, 10, 50, 30), interactable=True, tier="a11y")]
            with patch.object(executor, "_run_t0", return_value=t0_nodes), \
                 patch.object(executor, "_run_t1", return_value=[]):
                perception = await executor.perceive()
        assert "Content:" in perception.spatial_context
        assert "Hello World" in perception.spatial_context
        assert "article body text" in perception.spatial_context

    @pytest.mark.asyncio
    async def test_perceive_tries_main_before_body(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.viewport_size = {"width": 1280, "height": 900}
        page.evaluate = AsyncMock(return_value=[])
        page.screenshot = AsyncMock(return_value=b"png")
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)

        call_log = []
        async def mock_inner_text(selector):
            call_log.append(selector)
            if selector == "main":
                return "Main content area with enough text to pass the 50 char threshold easily."
            return "Body fallback"

        page.inner_text = mock_inner_text
        cdp = AsyncMock()
        with patch("bridges.web_bridge.executor.WebExecutor._get_cdp_session", return_value=cdp):
            executor = WebExecutor(page, "test-session")
            t0_nodes = [UIDNode(uid="_pending", tag="button", text="OK", bbox=(10, 10, 50, 30), interactable=True, tier="a11y")]
            with patch.object(executor, "_run_t0", return_value=t0_nodes), \
                 patch.object(executor, "_run_t1", return_value=[]):
                perception = await executor.perceive()
        assert "Main content area" in perception.spatial_context
        assert "main" in call_log  # tried 'main' selector

    @pytest.mark.asyncio
    async def test_perceive_truncates_long_text(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.viewport_size = {"width": 1280, "height": 900}
        page.evaluate = AsyncMock(return_value=[])
        page.screenshot = AsyncMock(return_value=b"png")
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(return_value="A" * 5000)
        cdp = AsyncMock()
        with patch("bridges.web_bridge.executor.WebExecutor._get_cdp_session", return_value=cdp):
            executor = WebExecutor(page, "test-session")
            with patch.object(executor, "_run_t0", return_value=[]), \
                 patch.object(executor, "_run_t1", return_value=[]):
                perception = await executor.perceive()
        content_start = perception.spatial_context.find("Content:\n")
        content = perception.spatial_context[content_start + len("Content:\n"):]
        assert content.endswith("...")
        assert len(content) <= 2010  # 2000 + "..."

    @pytest.mark.asyncio
    async def test_perceive_no_content_on_failure(self):
        from bridges.web_bridge.executor import WebExecutor
        page = AsyncMock()
        page.url = "https://example.com"
        page.title = AsyncMock(return_value="Example")
        page.viewport_size = {"width": 1280, "height": 900}
        page.evaluate = AsyncMock(return_value=[])
        page.screenshot = AsyncMock(return_value=b"png")
        page.accessibility = MagicMock()
        page.accessibility.snapshot = AsyncMock(return_value=None)
        page.inner_text = AsyncMock(side_effect=Exception("DOM detached"))
        cdp = AsyncMock()
        with patch("bridges.web_bridge.executor.WebExecutor._get_cdp_session", return_value=cdp):
            executor = WebExecutor(page, "test-session")
            with patch.object(executor, "_run_t0", return_value=[]), \
                 patch.object(executor, "_run_t1", return_value=[]):
                perception = await executor.perceive()
        assert "Content:" not in perception.spatial_context  # graceful fallback
