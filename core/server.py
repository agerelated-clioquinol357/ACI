"""
OpenClaw 2.0 ACI Framework — FastAPI Server.

The ONLY interface LLM agents interact with.  Provides REST endpoints for
session management, action execution, and environment perception, plus a
WebSocket endpoint for bridge worker connections.
"""

from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
from contextlib import asynccontextmanager
from typing import Any, Optional

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from pydantic import BaseModel
import uvicorn

from .session_manager import SessionManager
from .protocol_router import ProtocolRouter
from .models.schemas import (
    ActionRequest,
    ActionResult,
    ContextPerception,
    SessionConfig,
    TaskState,
    UIInterruptEvent,
)
from memory_core import knowledge_base as kb

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

_PID_FILE = pathlib.Path(__file__).resolve().parent.parent / ".openclaw_daemon.pid"


def _check_and_write_pid() -> None:
    """Ensure only one daemon instance runs at a time via a PID file."""
    if _PID_FILE.exists():
        try:
            old_pid = int(_PID_FILE.read_text().strip())
            # Check if the old process is still alive.
            try:
                os.kill(old_pid, 0)  # signal 0 = existence check
                logger.error(
                    "Another daemon is already running (PID %d). "
                    "Run stop_aci.ps1 first or delete %s.",
                    old_pid, _PID_FILE,
                )
                sys.exit(1)
            except OSError:
                # Process is gone — stale PID file, safe to overwrite.
                logger.info("Removing stale PID file (old PID %d).", old_pid)
        except (ValueError, IOError):
            pass  # Corrupt PID file — overwrite it.
    _PID_FILE.write_text(str(os.getpid()))
    logger.info("PID file written: %s (PID %d)", _PID_FILE, os.getpid())


def _remove_pid() -> None:
    """Remove the PID file on shutdown."""
    try:
        if _PID_FILE.exists() and _PID_FILE.read_text().strip() == str(os.getpid()):
            _PID_FILE.unlink()
            logger.info("PID file removed.")
    except Exception:
        pass


def _load_config() -> dict[str, Any]:
    """Return daemon configuration from environment variables or hardcoded defaults."""
    return {
        "version": "2.0.0",
        "daemon": {
            "host": os.environ.get("OPENCLAW_DAEMON_HOST", "127.0.0.1"),
            "port": int(os.environ.get("OPENCLAW_DAEMON_PORT", "11434")),
            "log_level": os.environ.get("OPENCLAW_LOG_LEVEL", "info"),
        },
    }


config = _load_config()
_daemon = config.get("daemon", {})


# ---------------------------------------------------------------------------
# Application lifespan
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    """Create shared SessionManager and ProtocolRouter on startup."""
    _check_and_write_pid()
    app.state.session_manager = SessionManager()
    app.state.protocol_router = ProtocolRouter()
    logger.info("OpenClaw ACI server starting (version %s)", config.get("version"))
    yield
    # Shutdown: close all sessions gracefully
    await app.state.session_manager.close_all()
    _remove_pid()
    logger.info("OpenClaw ACI server shut down")


app = FastAPI(
    title="OpenClaw ACI Server",
    version=config.get("version", "2.0.0"),
    lifespan=lifespan,
)

# Configure logging based on config
logging.basicConfig(
    level=getattr(logging, _daemon.get("log_level", "info").upper(), logging.INFO),
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)


# ---------------------------------------------------------------------------
# Helper accessors
# ---------------------------------------------------------------------------

def _sm() -> SessionManager:
    return app.state.session_manager


def _router() -> ProtocolRouter:
    return app.state.protocol_router


# ---------------------------------------------------------------------------
# Request / Response models for endpoints that aren't covered by schemas.py
# ---------------------------------------------------------------------------

class PerceiveRequest(BaseModel):
    session_id: str
    context_env: str = "web"
    region: Optional[list[int]] = None  # Optional ROI as [x, y, width, height]


class ScreenshotRequest(BaseModel):
    session_id: str
    context_env: str = "web"


class KnowledgeQueryRequest(BaseModel):
    app_name: Optional[str] = None
    window_class: Optional[str] = None
    process_name: Optional[str] = None


# ---------------------------------------------------------------------------
# REST endpoints
# ---------------------------------------------------------------------------

@app.post("/v1/session")
async def create_session(body: SessionConfig):
    """Create a new ACI session.

    If ``target_url`` is provided and the corresponding bridge is connected,
    the bridge is immediately instructed to navigate to that URL.
    """
    try:
        session_id = await _sm().create_session(body)
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    # If a target_url is specified, tell the bridge to navigate now.
    if body.target_url and _router().is_bridge_connected(body.context_env):
        try:
            nav_result = await _router().send_navigate(
                body.context_env, session_id, body.target_url,
            )
            logger.info(
                "Session '%s': navigated to %s (result=%s)",
                session_id, body.target_url, nav_result,
            )
        except Exception as exc:
            logger.warning(
                "Session '%s': navigation to %s failed: %s",
                session_id, body.target_url, exc,
            )

    return {"session_id": session_id, "status": "created"}


@app.post("/v1/action")
async def execute_action(action: ActionRequest):
    """Execute an action — the main LLM endpoint.

    Acquires the per-session I/O lock, transitions through the state machine,
    dispatches to the appropriate bridge, and returns the result.
    """
    sm = _sm()
    router = _router()
    state_machine = sm.get_state_machine()
    session_id = action.session_id

    # Validate session exists and touch activity timestamp
    try:
        session_info = await sm.get_session(session_id)
        await sm.touch_session(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    # Acquire I/O lock to serialize physical input
    try:
        await sm.acquire_io(session_id)
    except KeyError:
        raise HTTPException(status_code=404, detail=f"Session '{session_id}' not found")

    try:
        # Auto-recover from FAILED/COMPLETED → IDLE before attempting
        # the IDLE → EXECUTING transition. Without this, a previous
        # error permanently locks the session.
        try:
            current_state = await state_machine.get_state(session_id)
            if current_state in (TaskState.FAILED, TaskState.COMPLETED):
                await state_machine.transition(session_id, TaskState.IDLE)
                logger.info(
                    "Session '%s' auto-recovered from %s to IDLE",
                    session_id, current_state.value,
                )
        except (KeyError, ValueError):
            pass  # Best-effort recovery

        # Transition to EXECUTING
        try:
            await state_machine.transition(session_id, TaskState.EXECUTING)
        except ValueError as exc:
            raise HTTPException(status_code=409, detail=str(exc))

        # Dispatch to bridge
        try:
            result = await router.dispatch_action(action)
        except ConnectionError as exc:
            # No bridge connected for this environment
            await state_machine.transition(session_id, TaskState.FAILED)
            raise HTTPException(status_code=503, detail=str(exc))

        # Check if the result indicates a UI interrupt
        if not result.success and result.error and "interrupt" in result.error.lower():
            # Treat as a UI interrupt event
            interrupt = UIInterruptEvent(
                session_id=session_id,
                interrupt_type="modal",
                description=result.error,
            )
            frozen_context = {
                "action": action.model_dump(),
                "partial_result": result.model_dump(),
            }
            interrupt_info = await state_machine.push_interrupt(
                session_id, interrupt, frozen_context
            )
            return {
                "status": "blocked",
                "interrupt": interrupt_info,
            }

        # Success path
        if result.success:
            await state_machine.transition(session_id, TaskState.IDLE)
        else:
            await state_machine.transition(session_id, TaskState.FAILED)

        return result.model_dump()

    except HTTPException:
        raise
    except Exception as exc:
        logger.error(
            "Unexpected error executing action for session '%s': %s",
            session_id, exc, exc_info=True,
        )
        try:
            await state_machine.transition(session_id, TaskState.FAILED)
        except (ValueError, KeyError):
            pass  # Best-effort state transition
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        try:
            await sm.release_io(session_id)
        except (KeyError, RuntimeError) as exc:
            logger.warning(
                "Could not release I/O lock for session '%s': %s",
                session_id, exc,
            )


@app.post("/v1/perceive")
async def perceive(body: PerceiveRequest):
    """Get the current environment state by asking the bridge to perceive."""
    sm = _sm()
    router = _router()
    state_machine = sm.get_state_machine()

    # Validate session
    try:
        await sm.get_session(body.session_id)
        await sm.touch_session(body.session_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Session '{body.session_id}' not found"
        )

    # Auto-recover from FAILED/COMPLETED so perceive always works.
    try:
        current = await state_machine.get_state(body.session_id)
        if current in (TaskState.FAILED, TaskState.COMPLETED):
            await state_machine.transition(body.session_id, TaskState.IDLE)
    except (KeyError, ValueError):
        pass

    # Request perception from the bridge
    try:
        bridge_data = await router.request_perception(
            body.context_env, body.session_id, region=body.region
        )
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error(
            "Perception failed for session '%s': %s",
            body.session_id, exc, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    # Combine bridge perception data with state machine state
    try:
        current_state = await state_machine.get_state(body.session_id)
    except KeyError:
        current_state = TaskState.IDLE

    perception = ContextPerception(
        state=current_state,
        session_id=body.session_id,
        context_env=body.context_env,
        active_window_title=bridge_data.get("active_window_title", ""),
        current_url=bridge_data.get("current_url"),
        elements=bridge_data.get("elements", []),
        interrupted_reason=bridge_data.get("interrupted_reason"),
        visual_reference_image=bridge_data.get("visual_reference_image"),
        app_knowledge=bridge_data.get("app_knowledge"),
        spatial_context=bridge_data.get("spatial_context"),
        last_action_result=bridge_data.get("last_action_result"),
    )

    return perception.model_dump()


@app.get("/v1/sessions")
async def list_sessions():
    """List all active sessions."""
    sessions = await _sm().list_sessions()
    return {"sessions": sessions}


@app.post("/v1/screenshot")
async def take_screenshot(body: ScreenshotRequest):
    """Capture a screenshot from the specified bridge.

    Returns base64-encoded PNG image data. Essential for capturing QR codes,
    verifying visual state, and supporting human-in-the-loop login flows.
    """
    sm = _sm()
    router = _router()

    try:
        await sm.get_session(body.session_id)
        await sm.touch_session(body.session_id)
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Session '{body.session_id}' not found"
        )

    try:
        result = await router.request_screenshot(body.context_env, body.session_id)
    except ConnectionError as exc:
        raise HTTPException(status_code=503, detail=str(exc))
    except Exception as exc:
        logger.error(
            "Screenshot failed for session '%s': %s",
            body.session_id, exc, exc_info=True,
        )
        raise HTTPException(status_code=500, detail=str(exc))

    return result


@app.delete("/v1/session/{session_id}")
async def close_session(session_id: str):
    """Close and clean up a session."""
    try:
        await _sm().close_session(session_id)
        return {"session_id": session_id, "status": "closed"}
    except KeyError:
        raise HTTPException(
            status_code=404, detail=f"Session '{session_id}' not found"
        )


@app.post("/api/v1/knowledge/query")
async def query_knowledge(body: KnowledgeQueryRequest):
    """Query the YAML app knowledge base by app name, window class, or process name."""
    app_name = body.app_name

    if not app_name and body.window_class:
        app_name = kb.find_by_window_class(body.window_class)
    if not app_name and body.process_name:
        app_name = kb.find_by_process_name(body.process_name)

    if not app_name:
        raise HTTPException(
            status_code=404,
            detail="Could not resolve app name from provided parameters.",
        )

    data = kb.load(app_name)
    if not data:
        raise HTTPException(
            status_code=404,
            detail=f"No knowledge base entry found for app '{app_name}'.",
        )

    return {"app_name": app_name, "knowledge": data}


@app.get("/health")
async def health():
    """Health check endpoint."""
    router = _router()
    return {
        "status": "ok",
        "version": config.get("version", "2.0.0"),
        "connected_bridges": router.list_connected_bridges(),
    }


# ---------------------------------------------------------------------------
# WebSocket endpoint for bridge workers
# ---------------------------------------------------------------------------

@app.websocket("/ws/bridge/{bridge_type}")
async def bridge_websocket(websocket: WebSocket, bridge_type: str):
    """WebSocket connection for bridge workers.

    On connect: register the bridge with the protocol router.
    On message: handle incoming events (e.g. UI_INTERRUPT from workers).
    On disconnect: unregister the bridge.
    """
    router = _router()
    sm = _sm()
    state_machine = sm.get_state_machine()

    await websocket.accept()
    await router.register_bridge(bridge_type, websocket)
    logger.info("Bridge worker connected: %s", bridge_type)

    try:
        while True:
            raw = await websocket.receive_text()

            try:
                message = json.loads(raw)
            except json.JSONDecodeError:
                logger.warning(
                    "Invalid JSON from bridge '%s': %s", bridge_type, raw[:200]
                )
                continue

            msg_type = message.get("type", "").lower()
            data = message.get("data", {})

            if msg_type == "register":
                # Bridge worker registration handshake — log and discard.
                logger.info(
                    "Bridge '%s' registration handshake received (session=%s)",
                    bridge_type, data.get("session_id", "?"),
                )
                continue

            elif msg_type == "ui_interrupt":
                # Bridge worker is reporting a UI interrupt event
                session_id = data.get("session_id")
                if not session_id:
                    logger.warning(
                        "UI_INTERRUPT from bridge '%s' missing session_id",
                        bridge_type,
                    )
                    continue

                try:
                    interrupt = UIInterruptEvent(
                        session_id=session_id,
                        interrupt_type=data.get("interrupt_type", "modal"),
                        description=data.get(
                            "description", "UI interrupt detected by bridge"
                        ),
                        blocking_element_uid=data.get("blocking_element_uid"),
                        screenshot_b64=data.get("screenshot_b64"),
                    )

                    frozen_context = data.get("frozen_context", {})
                    interrupt_info = await state_machine.push_interrupt(
                        session_id, interrupt, frozen_context
                    )
                    logger.info(
                        "UI interrupt pushed for session '%s' from bridge '%s': %s",
                        session_id, bridge_type, interrupt.interrupt_type,
                    )

                    # Acknowledge back to the bridge
                    await websocket.send_text(json.dumps({
                        "type": "interrupt_ack",
                        "data": interrupt_info,
                    }, ensure_ascii=False))
                except (KeyError, ValueError) as exc:
                    logger.error(
                        "Failed to process UI_INTERRUPT for session '%s': %s",
                        session_id, exc,
                    )
                    await websocket.send_text(json.dumps({
                        "type": "error",
                        "message": str(exc),
                    }, ensure_ascii=False))
            elif msg_type == "error":
                # Error response from the bridge — still route to the
                # response queue so dispatch_action can handle it instead
                # of timing out.  dispatch_action checks type=="error".
                logger.warning(
                    "Bridge '%s' error: %s",
                    bridge_type, data.get("error", message.get("message", "?")),
                )
                bridge_conn = await router.get_bridge(bridge_type)
                await bridge_conn.put_response(raw)

            else:
                # All other messages (result, perception responses, etc.)
                # are routed to the response queue for dispatch_action / request_perception
                bridge_conn = await router.get_bridge(bridge_type)
                await bridge_conn.put_response(raw)

    except WebSocketDisconnect as exc:
        logger.warning(
            "Bridge worker disconnected: %s (code=%s, reason=%s)",
            bridge_type, getattr(exc, "code", "?"), getattr(exc, "reason", "?"),
        )
    except Exception as exc:
        logger.error(
            "Bridge WebSocket error for '%s': %s", bridge_type, exc, exc_info=True
        )
    finally:
        await router.unregister_bridge(bridge_type)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    uvicorn.run(
        "core.server:app",
        host=_daemon.get("host", "127.0.0.1"),
        port=_daemon.get("port", 11434),
        reload=True,
    )
