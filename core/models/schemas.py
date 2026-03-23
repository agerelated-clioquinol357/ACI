"""OpenClaw 2.0 ACI Framework - Data Contract Schemas.

Single Source of Truth for ALL data contracts in the OpenClaw ACI system.
Every model is strictly validated using Pydantic V2.

Models defined here:
    - TaskState: Enum of possible task lifecycle states.
    - UIDNode: A single interactable UI element in the accessibility tree.
    - ActionRequest: Payload the LLM sends to OpenClaw to perform an action.
    - ActionResult: Outcome of an executed action.
    - ContextPerception: Full environment snapshot returned to the LLM.
    - UIInterruptEvent: Anomaly / interrupt event fired by bridge workers.
    - SessionConfig: Configuration for creating a new ACI session.
"""

from __future__ import annotations

import re
import time
from enum import Enum
from typing import Literal, Optional

from pydantic import BaseModel, Field, field_validator, model_validator


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

class TaskState(str, Enum):
    """Lifecycle states for a single ACI task."""

    IDLE = "idle"
    EXECUTING = "executing"
    BLOCKED_BY_UI = "blocked_by_ui"
    AWAIT_SUBTASK = "await_subtask"
    FAILED = "failed"
    COMPLETED = "completed"


# ---------------------------------------------------------------------------
# Core Models
# ---------------------------------------------------------------------------

class UIDNode(BaseModel):
    """Represents a single interactable UI element in the accessibility tree.

    Each node is assigned a unique ``uid`` (e.g. ``"oc_42"``) that the LLM
    uses to reference the element when issuing actions.
    """

    uid: str = Field(
        ...,
        min_length=1,
        description='Unique element identifier, e.g. "oc_42".',
    )
    tag: str = Field(
        ...,
        min_length=1,
        description='HTML tag or native control type, e.g. "button", "input".',
    )
    role: Optional[str] = Field(
        default=None,
        description="ARIA role or UIA control type, if available.",
    )
    text: str = Field(
        ...,
        max_length=200,
        description="Visible text content, trimmed to 200 characters.",
    )
    attributes: dict[str, str] = Field(
        default_factory=dict,
        description="Relevant HTML/UIA attributes (href, placeholder, aria-label, etc.).",
    )
    bbox: Optional[tuple[int, int, int, int]] = Field(
        default=None,
        description="Bounding box as (x, y, width, height) in screen pixels.",
    )
    interactable: bool = Field(
        default=True,
        description="Whether the element is currently interactable.",
    )
    tier: Optional[str] = Field(
        default=None,
        description="Detection tier that found this element (e.g. 'uia', 'cursor_probe', 'ocr', 'contour', 'vlm').",
    )

    @field_validator("text", mode="before")
    @classmethod
    def _trim_text(cls, v: str) -> str:
        """Strip leading/trailing whitespace from visible text."""
        if isinstance(v, str):
            return v.strip()
        return v


class ActionRequest(BaseModel):
    """Payload the LLM sends to OpenClaw to request a UI action.

    The ``action_type`` determines which bridge method is invoked.
    ``target_uid`` is mandatory for element-specific actions (click, type,
    select, hover) and optional for global actions (scroll, press_key, wait).
    """

    session_id: str = Field(
        ...,
        min_length=1,
        description="Session this action belongs to.",
    )
    action_type: Literal[
        "click", "type", "press_key", "scroll", "wait", "select", "hover", "launch_app",
        "execute_js", "click_selector", "sequence",
    ] = Field(
        ...,
        description="The kind of action to perform.",
    )
    target_uid: Optional[str] = Field(
        default=None,
        description="UID of the target element. Required for click/type/select/hover.",
    )
    value: Optional[str] = Field(
        default=None,
        description="Text to type, key to press, or scroll direction.",
    )
    context_env: Literal["web", "desktop", "cli"] = Field(
        default="web",
        description="Execution environment that determines which bridge handles the action.",
    )
    force_fallback: bool = Field(
        default=False,
        description="When True, forces the T3 vision-based fallback path.",
    )
    request_id: Optional[str] = Field(
        default=None,
        description="Internal request ID for response correlation.",
    )

    @model_validator(mode="after")
    def _require_target_for_element_actions(self) -> ActionRequest:
        """Ensure ``target_uid`` is provided for element-specific actions."""
        element_actions = {"click", "type", "select", "hover"}
        if self.action_type in element_actions and not self.target_uid and not self.force_fallback:
            raise ValueError(
                f"'target_uid' is required for '{self.action_type}' actions "
                f"(or set force_fallback=True)."
            )
        return self


class ActionResult(BaseModel):
    """Result returned after executing an :class:`ActionRequest`.

    Every action produces exactly one ``ActionResult`` regardless of
    success or failure.
    """

    success: bool = Field(
        ...,
        description="Whether the action completed successfully.",
    )
    action_type: str = Field(
        ...,
        min_length=1,
        description="The action type that was executed.",
    )
    target_uid: Optional[str] = Field(
        default=None,
        description="UID of the element acted upon, if applicable.",
    )
    message: str = Field(
        default="",
        description="Human-readable feedback about the action outcome.",
    )
    error: Optional[str] = Field(
        default=None,
        description="Error description when success is False.",
    )
    elapsed_ms: float = Field(
        default=0.0,
        ge=0.0,
        description="Wall-clock time in milliseconds the action took.",
    )
    verification_screenshot: Optional[str] = Field(
        default=None,
        description="Post-action verification screenshot. Value is a file path string like '[screenshot saved: /path/to/file.png]' (not raw base64).",
    )
    ui_change_detected: Optional[bool] = Field(
        default=None,
        description="Whether visual change was detected after the action. None = not measured.",
    )


class ContextPerception(BaseModel):
    """Full environment snapshot sent to the LLM after each action cycle.

    Combines the current task state, the live accessibility tree
    (``elements``), and the result of the most recent action.
    """

    state: TaskState = Field(
        ...,
        description="Current lifecycle state of the task.",
    )
    session_id: str = Field(
        ...,
        min_length=1,
        description="Owning session identifier.",
    )
    active_window_title: str = Field(
        default="",
        description="Title of the currently focused window.",
    )
    context_env: Literal["web", "desktop", "cli"] = Field(
        default="web",
        description="Execution environment.",
    )
    current_url: Optional[str] = Field(
        default=None,
        description="Current page URL (web context only).",
    )
    elements: list[UIDNode] = Field(
        default_factory=list,
        description="Interactable UI elements in the current view.",
    )
    interrupted_reason: Optional[str] = Field(
        default=None,
        description="Reason the task was interrupted, if applicable.",
    )
    visual_reference_image: Optional[str] = Field(
        default=None,
        description="Annotated vision screenshot. Value is a file path string like '[screenshot saved: /path/to/file.png]' (not raw base64).",
    )
    app_knowledge: Optional[dict] = Field(
        default=None,
        description="当前app的快捷键、已知UI模式和操作指南，来自YAML知识库。agent应优先使用快捷键完成操作。",
    )
    spatial_context: Optional[str] = Field(
        default=None,
        description="人类可读的元素空间布局描述，帮助agent理解UI结构。",
    )
    last_action_result: Optional[ActionResult] = Field(
        default=None,
        description="Result of the most recently executed action.",
    )


class UIInterruptEvent(BaseModel):
    """Event fired by bridge workers when an anomaly is detected.

    Examples include unexpected modals, authentication dialogs, cookie
    overlays, or navigation redirects that block the planned action flow.
    """

    session_id: str = Field(
        ...,
        min_length=1,
        description="Session in which the interrupt occurred.",
    )
    interrupt_type: Literal[
        "modal", "dialog", "overlay", "redirect", "error"
    ] = Field(
        ...,
        description="Category of the interrupt.",
    )
    description: str = Field(
        ...,
        min_length=1,
        description="Human-readable description of the anomaly.",
    )
    blocking_element_uid: Optional[str] = Field(
        default=None,
        description="UID of the element causing the block, if identifiable.",
    )
    screenshot_b64: Optional[str] = Field(
        default=None,
        description="Base64-encoded screenshot captured at interrupt time.",
    )
    timestamp: float = Field(
        default_factory=time.time,
        description="Unix timestamp when the interrupt was detected.",
    )


class SessionConfig(BaseModel):
    """Configuration payload for creating a new ACI session.

    The ``session_id`` is user-supplied and must contain only alphanumeric
    characters, hyphens, and underscores.
    """

    session_id: str = Field(
        ...,
        min_length=1,
        description="Unique session identifier (alphanumeric, hyphens, underscores).",
    )
    context_env: Literal["web", "desktop", "cli"] = Field(
        default="web",
        description="Execution environment for this session.",
    )
    target_url: Optional[str] = Field(
        default=None,
        description="Initial URL to navigate to (web bridge only).",
    )
    working_dir: Optional[str] = Field(
        default=None,
        description="Working directory path (CLI bridge only).",
    )
    metadata: dict[str, str] = Field(
        default_factory=dict,
        description="Arbitrary key-value metadata for the session.",
    )

    @field_validator("session_id")
    @classmethod
    def _validate_session_id(cls, v: str) -> str:
        """Ensure session_id contains only alphanumeric chars, hyphens, and underscores."""
        if not re.fullmatch(r"[A-Za-z0-9\-_]+", v):
            raise ValueError(
                "session_id must contain only alphanumeric characters, "
                f"hyphens, and underscores. Got: {v!r}"
            )
        return v
