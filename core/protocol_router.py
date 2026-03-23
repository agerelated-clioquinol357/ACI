"""
OpenClaw 2.0 ACI Framework — Protocol Router.

Routes ActionRequests to the correct bridge worker via WebSocket connections.
Uses per-request asyncio.Future objects keyed by request_id to correlate
responses from the bridge, avoiding queue desynchronisation.
"""

import asyncio
import json
import logging
import uuid
from typing import Any, Optional

from .models.schemas import ActionRequest, ActionResult

logger = logging.getLogger(__name__)


class BridgeConnection:
    """Wraps a bridge's WebSocket and provides request-response correlation."""

    def __init__(self, websocket: Any):
        self.websocket = websocket
        # Pending requests: request_id → Future[str]
        self._pending: dict[str, asyncio.Future[str]] = {}
        # Fallback queue for messages without a request_id (legacy compat).
        self._fallback_queue: asyncio.Queue[str] = asyncio.Queue()

    async def send(self, payload: str) -> None:
        send_fn = getattr(self.websocket, "send_text", None) or self.websocket.send
        await send_fn(payload)

    def register_request(self, request_id: str) -> asyncio.Future:
        """Register a pending request and return a Future for the response."""
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[str] = loop.create_future()
        self._pending[request_id] = fut
        return fut

    async def wait_response(self, request_id: str, timeout: float = 30.0) -> str:
        """Wait for the response that matches *request_id*."""
        fut = self._pending.get(request_id)
        if fut is None:
            # Fallback for callers that didn't register a request.
            return await asyncio.wait_for(self._fallback_queue.get(), timeout=timeout)
        try:
            return await asyncio.wait_for(fut, timeout=timeout)
        finally:
            self._pending.pop(request_id, None)

    async def put_response(self, raw: str) -> None:
        """Route a response to the matching pending request or the fallback queue."""
        try:
            msg = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            msg = {}
        # Check for request_id in the response data envelope.
        req_id = msg.get("request_id") or msg.get("data", {}).get("request_id")
        if req_id and req_id in self._pending:
            fut = self._pending[req_id]
            if not fut.done():
                fut.set_result(raw)
            return
        # No matching request_id → fallback queue.
        await self._fallback_queue.put(raw)


class ProtocolRouter:
    """Routes ActionRequests to the correct bridge worker via WebSocket connections."""

    def __init__(self):
        self._bridges: dict[str, BridgeConnection] = {}
        self._lock = asyncio.Lock()

    async def register_bridge(self, bridge_type: str, websocket) -> None:
        async with self._lock:
            self._bridges[bridge_type] = BridgeConnection(websocket)
            logger.info(f"Bridge registered: {bridge_type}")

    async def unregister_bridge(self, bridge_type: str) -> None:
        async with self._lock:
            self._bridges.pop(bridge_type, None)
            logger.info(f"Bridge unregistered: {bridge_type}")

    async def get_bridge(self, bridge_type: str) -> BridgeConnection:
        bridge = self._bridges.get(bridge_type)
        if bridge is None:
            raise ConnectionError(f"No bridge connected for type: {bridge_type}")
        return bridge

    def is_bridge_connected(self, bridge_type: str) -> bool:
        return bridge_type in self._bridges

    def list_connected_bridges(self) -> list[str]:
        return list(self._bridges.keys())

    async def dispatch_action(self, action: ActionRequest) -> ActionResult:
        """Send an action to the appropriate bridge and wait for the result."""
        bridge = await self.get_bridge(action.context_env)
        req_id = uuid.uuid4().hex[:12]
        fut = bridge.register_request(req_id)

        data = action.model_dump()
        data["request_id"] = req_id
        payload = json.dumps({"type": "action", "data": data}, ensure_ascii=False)
        await bridge.send(payload)

        # Vision actions may involve tier-4 VLM calls (~5s).
        # T2-cached actions complete in <1s.
        raw = await bridge.wait_response(req_id, timeout=180.0)
        response = json.loads(raw)

        if response.get("type") == "error":
            return ActionResult(
                success=False,
                action_type=action.action_type,
                target_uid=action.target_uid,
                error=response.get("message", "Unknown bridge error"),
            )

        return ActionResult(**response.get("data", {}))

    async def send_navigate(self, bridge_type: str, session_id: str, url: str) -> dict:
        """Tell a bridge to navigate to the given URL."""
        bridge = await self.get_bridge(bridge_type)
        req_id = uuid.uuid4().hex[:12]
        fut = bridge.register_request(req_id)
        payload = json.dumps({
            "type": "navigate",
            "data": {"session_id": session_id, "url": url, "request_id": req_id},
        }, ensure_ascii=False)
        await bridge.send(payload)
        raw = await bridge.wait_response(req_id, timeout=120.0)
        return json.loads(raw).get("data", {})

    async def request_perception(self, bridge_type: str, session_id: str, region: list[int] | None = None) -> dict:
        """Ask a bridge to perceive the current environment."""
        bridge = await self.get_bridge(bridge_type)
        req_id = uuid.uuid4().hex[:12]
        fut = bridge.register_request(req_id)
        data: dict = {"session_id": session_id, "request_id": req_id}
        if region:
            data["region"] = region
        payload = json.dumps({"type": "perceive", "data": data}, ensure_ascii=False)
        await bridge.send(payload)
        raw = await bridge.wait_response(req_id, timeout=180.0)
        return json.loads(raw).get("data", {})

    async def request_screenshot(self, bridge_type: str, session_id: str) -> dict:
        """Ask a bridge to capture a screenshot."""
        bridge = await self.get_bridge(bridge_type)
        req_id = uuid.uuid4().hex[:12]
        fut = bridge.register_request(req_id)
        payload = json.dumps({
            "type": "screenshot",
            "data": {"session_id": session_id, "request_id": req_id},
        }, ensure_ascii=False)
        await bridge.send(payload)
        raw = await bridge.wait_response(req_id, timeout=15.0)
        return json.loads(raw).get("data", {})
