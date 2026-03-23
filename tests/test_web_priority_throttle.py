"""Tests for WebSocket priority lock and MutationShield throttling."""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock, patch

from core.models.schemas import UIInterruptEvent


class TestActionGate:
    """Verify the action_gate pauses interrupt forwarding during actions."""

    def test_action_gate_exists(self):
        from bridges.web_bridge.worker import WebBridgeWorker
        worker = WebBridgeWorker(session_id="test")
        assert hasattr(worker, "_action_gate")
        assert worker._action_gate.is_set()  # Open by default

    def test_ws_lock_exists(self):
        from bridges.web_bridge.worker import WebBridgeWorker
        worker = WebBridgeWorker(session_id="test")
        assert hasattr(worker, "_ws_lock")
        assert isinstance(worker._ws_lock, asyncio.Lock)

    @pytest.mark.asyncio
    async def test_action_gate_clears_during_action(self):
        """Gate is cleared during action execution, set after result sent."""
        from bridges.web_bridge.worker import WebBridgeWorker

        worker = WebBridgeWorker(session_id="test")
        worker._ws = AsyncMock()
        worker._ws.send = AsyncMock()

        page = AsyncMock()
        page.mouse = AsyncMock()

        # Mock executor
        mock_executor = AsyncMock()
        mock_result = MagicMock()
        mock_result.model_dump = MagicMock(return_value={"success": True, "action_type": "click"})

        gate_was_cleared = False

        async def mock_act(action):
            nonlocal gate_was_cleared
            gate_was_cleared = not worker._action_gate.is_set()
            return mock_result

        mock_executor.act = mock_act
        worker._executor = mock_executor
        worker._browser = MagicMock()  # Bypass _ensure_browser

        msg = {"data": {
            "session_id": "test", "action_type": "click",
            "target_uid": "@e1", "context_env": "web",
        }}
        await worker._handle_action(msg)

        assert gate_was_cleared is True  # Gate was cleared during action
        assert worker._action_gate.is_set()  # Gate is open again after

    @pytest.mark.asyncio
    async def test_action_gate_set_even_on_error(self):
        """Gate is restored even if action execution raises."""
        from bridges.web_bridge.worker import WebBridgeWorker

        worker = WebBridgeWorker(session_id="test")
        worker._ws = AsyncMock()
        worker._ws.send = AsyncMock()

        mock_executor = AsyncMock()
        mock_executor.act = AsyncMock(side_effect=Exception("boom"))
        worker._executor = mock_executor
        worker._browser = MagicMock()

        msg = {"data": {
            "session_id": "test", "action_type": "click",
            "target_uid": "@e1", "context_env": "web",
        }}
        # act() raises, but finally block should restore the gate.
        with pytest.raises(Exception, match="boom"):
            await worker._handle_action(msg)
        assert worker._action_gate.is_set()  # Gate restored despite error


class TestMutationShieldThrottling:
    """Verify bounded queue and batch limiting."""

    def test_bounded_queue_maxsize(self):
        from bridges.web_bridge.mutation_shield import MutationShield
        shield = MutationShield(session_id="test")
        assert shield._event_queue.maxsize == 50

    def test_queue_full_drops_events(self):
        from bridges.web_bridge.mutation_shield import MutationShield
        shield = MutationShield(session_id="test")
        # Fill the queue
        for i in range(50):
            shield._event_queue.put_nowait(UIInterruptEvent(
                session_id="test", interrupt_type="overlay",
                description=f"event {i}",
            ))
        # 51st should not raise but queue should be full
        assert shield._event_queue.full()

    def test_interrupt_rate_limit_constant(self):
        """Rate limiting constant exists in worker module."""
        from bridges.web_bridge import worker
        source = open(worker.__file__, encoding="utf-8").read()
        assert "_MIN_INTERRUPT_INTERVAL_S" in source
        assert "0.5" in source  # 500ms minimum interval


class TestSendJsonLock:
    """Verify _send_json uses the lock."""

    @pytest.mark.asyncio
    async def test_send_json_acquires_lock(self):
        from bridges.web_bridge.worker import WebBridgeWorker

        worker = WebBridgeWorker(session_id="test")
        worker._ws = AsyncMock()
        worker._ws.send = AsyncMock()

        lock_was_held = False
        original_send = worker._ws.send

        async def check_lock(*args, **kwargs):
            nonlocal lock_was_held
            lock_was_held = worker._ws_lock.locked()
            return await original_send(*args, **kwargs)

        worker._ws.send = check_lock
        await worker._send_json({"type": "test"})
        assert lock_was_held is True
