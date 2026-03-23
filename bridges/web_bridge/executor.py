"""
OpenClaw 2.0 ACI Framework - Web Bridge Executor (v2 Tiered Edition).

Tiered extraction: T0 CDP a11y tree, T1 DOM supplement, T2 vision fallback.
Coordinate-first action execution with frame-aware routing.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from pathlib import Path
from typing import Optional

from playwright.async_api import Page, Error as PlaywrightError

from core.models.schemas import (
    ActionRequest, ActionResult, ContextPerception, TaskState, UIDNode,
)
from .a11y_extractor import A11yExtractor
from .frame_manager import RefCounter, FrameManager
from .hover_prober import HoverProber, _is_trigger
from .vision_fallback import WebVisionFallback
from .snapshot_formatter import format_page_summary

logger = logging.getLogger(__name__)
_DOM_PARSER_JS = Path(__file__).parent / "dom_parser.js"
_DOM_SUPPLEMENT_JS = Path(__file__).parent / "dom_supplement.js"

_T2_THRESHOLD = 5
_PW_ACTION_TIMEOUT = float(os.environ.get("OPENCLAW_PW_ACTION_TIMEOUT", "10.0"))
_PAGE_TEXT_MAX_CHARS = int(os.environ.get("OPENCLAW_PAGE_TEXT_MAX", "2000"))


async def _pw_call(coro, label: str = "playwright"):
    """Wrap a Playwright awaitable in a timeout to fail fast on browser stalls."""
    try:
        return await asyncio.wait_for(coro, timeout=_PW_ACTION_TIMEOUT)
    except asyncio.TimeoutError:
        raise asyncio.TimeoutError(
            f"Playwright timeout ({_PW_ACTION_TIMEOUT}s) during {label}. "
            f"Browser may be under heavy load."
        )


def _iou(a: tuple, b: tuple) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[0] + a[2], b[0] + b[2])
    y2 = min(a[1] + a[3], b[1] + b[3])
    if x2 <= x1 or y2 <= y1:
        return 0.0
    inter = (x2 - x1) * (y2 - y1)
    area_a = a[2] * a[3]
    area_b = b[2] * b[3]
    denom = area_a + area_b - inter
    return inter / denom if denom > 0 else 0.0


class WebExecutor:
    def __init__(self, page: Page, session_id: str) -> None:
        self._page: Page = page
        self._session_id: str = session_id
        self._dom_parser_source: Optional[str] = None
        self._dom_supplement_source: Optional[str] = None
        self._cdp = None
        self._a11y_extractor: Optional[A11yExtractor] = None
        self._vision_fallback = WebVisionFallback()
        self._hover_prober = HoverProber()
        self._frame_manager = FrameManager()
        self._ref_counter = RefCounter()

    async def _get_cdp_session(self):
        if self._cdp is None:
            self._cdp = await self._page.context.new_cdp_session(self._page)
        return self._cdp

    def _load_dom_parser(self) -> str:
        if self._dom_parser_source is None:
            self._dom_parser_source = _DOM_PARSER_JS.read_text(encoding="utf-8")
        return self._dom_parser_source

    def _load_dom_supplement(self) -> str:
        if self._dom_supplement_source is None:
            self._dom_supplement_source = _DOM_SUPPLEMENT_JS.read_text(encoding="utf-8")
        return self._dom_supplement_source

    async def perceive(self) -> ContextPerception:
        self._ref_counter.reset()
        vp = self._page.viewport_size or {"width": 1280, "height": 900}
        vw, vh = vp["width"], vp["height"]

        try:
            cdp = await self._get_cdp_session()
        except Exception as exc:
            logger.warning("WebExecutor: CDP failed (%s), using legacy", exc)
            return await self._perceive_legacy()

        try:
            self._a11y_extractor = A11yExtractor(viewport_width=vw, viewport_height=vh)
            t0_nodes, t1_nodes = await asyncio.gather(
                self._run_t0(cdp),
                self._run_t1(vw, vh),
            )
        except Exception as exc:
            logger.warning("WebExecutor: tiered extraction failed (%s), using legacy", exc)
            return await self._perceive_legacy()

        # Merge with IoU dedup
        t0_bboxes = [n.bbox for n in t0_nodes if n.bbox]
        deduped_t1 = []
        for t1n in t1_nodes:
            if not t1n.bbox:
                deduped_t1.append(t1n)
                continue
            is_dup = any(_iou(t1n.bbox, t0b) > 0.7 for t0b in t0_bboxes)
            if not is_dup:
                deduped_t1.append(t1n)

        all_nodes = list(t0_nodes) + deduped_t1
        all_nodes = self._ref_counter.assign_refs(all_nodes, frame_id="main")

        # T2 vision fallback (mutually exclusive with hover)
        t2_fired = False
        if len(all_nodes) < _T2_THRESHOLD:
            t2_fired = True
            try:
                screenshot = await self._page.screenshot(type="png")
                vision_nodes = await self._vision_fallback.extract(screenshot)
                vision_refs = self._ref_counter.assign_refs(vision_nodes, frame_id="main")
                all_nodes.extend(vision_refs)
            except Exception as exc:
                logger.debug("WebExecutor: T2 failed: %s", exc)

        # Hover probing (only if T2 did NOT fire)
        if not t2_fired:
            triggers = [n for n in all_nodes if _is_trigger(n)]
            if triggers:
                try:
                    async def _extract_new():
                        new_t0 = await self._run_t0(cdp)
                        existing_bboxes = [n.bbox for n in all_nodes if n.bbox]
                        return [n for n in new_t0 if not n.bbox or not any(_iou(n.bbox, eb) > 0.7 for eb in existing_bboxes)]

                    hover_nodes = await self._hover_prober.probe(self._page, triggers, _extract_new)
                    hover_refs = self._ref_counter.assign_refs(hover_nodes, frame_id="main")
                    all_nodes.extend(hover_refs)
                except Exception as exc:
                    logger.debug("WebExecutor: hover probing failed: %s", exc)

        # iframe extraction
        try:
            frame_infos = await self._frame_manager.discover_frames(self._page)
            for fi in frame_infos:
                if fi["is_same_origin"]:
                    try:
                        frame_nodes = await self._extract_frame_elements(fi)
                        frame_refs = self._ref_counter.assign_refs(frame_nodes, frame_id=fi["frame_id"])
                        all_nodes.extend(frame_refs)
                    except Exception as exc:
                        logger.debug("WebExecutor: iframe failed for %s: %s", fi["frame_id"], exc)
        except Exception as exc:
            logger.debug("WebExecutor: frame discovery failed: %s", exc)

        logger.debug("WebExecutor: tiered perceive complete, %d elements", len(all_nodes))
        title = await self._page.title()
        url = self._page.url
        snapshot = format_page_summary(url, title, all_nodes)

        # Page text content via inner_text (~6ms).
        # Gives agents readable prose (paragraphs, comments, article text)
        # that the interactive-only element list cannot provide.
        try:
            for selector in ("main", "article", '[role="main"]', "body"):
                try:
                    page_text = await _pw_call(
                        self._page.inner_text(selector), "inner_text"
                    )
                    if page_text and len(page_text.strip()) > 50:
                        break
                except Exception:
                    page_text = ""
            if page_text:
                page_text = page_text.strip()
                if len(page_text) > _PAGE_TEXT_MAX_CHARS:
                    page_text = page_text[:_PAGE_TEXT_MAX_CHARS] + "..."
                snapshot += "\n\nContent:\n" + page_text
        except Exception as exc:
            logger.debug("WebExecutor: page text extraction failed: %s", exc)

        return ContextPerception(
            state=TaskState.IDLE,
            session_id=self._session_id,
            active_window_title=title,
            context_env="web",
            current_url=url,
            elements=all_nodes,
            spatial_context=snapshot,
        )

    async def _run_t0(self, cdp) -> list[UIDNode]:
        return await self._a11y_extractor.extract(cdp)

    async def _run_t1(self, vw: int, vh: int) -> list[UIDNode]:
        try:
            await self._page.evaluate(self._load_dom_supplement())
            t0_bboxes = []
            raw = await self._page.evaluate(
                "window.OpenClawSupplement.extractSupplement(arguments[0])", t0_bboxes
            )
            nodes = []
            for item in (raw or []):
                nodes.append(UIDNode(
                    uid="_pending",
                    tag=item.get("tag", "unknown"),
                    text=item.get("text", "")[:200],
                    attributes=item.get("attrs", {}),
                    bbox=tuple(item["bbox"]) if item.get("bbox") else None,
                    interactable=True,
                    tier="dom",
                ))
            return nodes
        except Exception as exc:
            logger.debug("WebExecutor: T1 failed: %s", exc)
            return []

    async def _extract_frame_elements(self, frame_info: dict) -> list[UIDNode]:
        frame = frame_info["frame"]
        try:
            frame_cdp = await self._page.context.new_cdp_session(frame)
            vp = self._page.viewport_size or {"width": 1280, "height": 900}
            extractor = A11yExtractor(viewport_width=vp["width"], viewport_height=vp["height"])
            nodes = await extractor.extract(frame_cdp)
            await frame_cdp.detach()
            return nodes
        except Exception:
            try:
                await frame.evaluate(self._load_dom_supplement())
                raw = await frame.evaluate("window.OpenClawSupplement.extractSupplement([])")
                return [
                    UIDNode(
                        uid="_pending", tag=item.get("tag", "unknown"),
                        text=item.get("text", "")[:200],
                        attributes={**item.get("attrs", {}), "frame_id": frame_info["frame_id"]},
                        bbox=tuple(item["bbox"]) if item.get("bbox") else None,
                        interactable=True, tier="dom",
                    )
                    for item in (raw or [])
                ]
            except Exception as exc:
                logger.debug("WebExecutor: iframe JS fallback failed: %s", exc)
                return []

    async def _perceive_legacy(self) -> ContextPerception:
        try:
            await self._page.evaluate(self._load_dom_parser())
            raw_elements = await self._page.evaluate("window.OpenClawExtractor.extractInteractables()")
            elements: list[UIDNode] = []
            for raw in raw_elements:
                ref = raw.get("ref", "")
                uid = raw.get("uid", "")
                bbox = raw.get("bbox")
                if ref and bbox:
                    self._ref_counter._ref_to_bbox[ref] = tuple(bbox)
                    self._ref_counter._ref_to_frame[ref] = "main"
                elements.append(UIDNode(
                    uid=ref if ref else "_legacy",
                    tag=raw.get("tag", "unknown"),
                    text=raw.get("text", ""),
                    attributes={**raw.get("attrs", {}), "real_uid": uid},
                    bbox=tuple(bbox) if bbox else None,
                    interactable=True,
                ))
            return ContextPerception(
                state=TaskState.IDLE, session_id=self._session_id,
                active_window_title=await self._page.title(),
                context_env="web", current_url=self._page.url, elements=elements,
            )
        except Exception as exc:
            logger.error("WebExecutor: legacy perception failed: %s", exc)
            return ContextPerception(
                state=TaskState.FAILED, session_id=self._session_id,
                context_env="web", interrupted_reason=str(exc),
            )

    async def act(self, action: ActionRequest) -> ActionResult:
        handler = self._ACTION_MAP.get(action.action_type)
        if not handler:
            return ActionResult(success=False, action_type=action.action_type, error=f"Unknown action: {action.action_type}")
        start = time.perf_counter()
        try:
            result = await handler(self, action)
            result.elapsed_ms = (time.perf_counter() - start) * 1000
            return result
        except Exception as exc:
            logger.error("WebExecutor: action '%s' failed: %s", action.action_type, exc)
            return ActionResult(success=False, action_type=action.action_type, error=str(exc))

    async def rollback(self) -> bool:
        logger.debug("WebExecutor: rollback requested (no-op)")
        return False

    async def _do_click(self, action: ActionRequest) -> ActionResult:
        bbox = self._ref_counter.get_bbox_for_ref(action.target_uid or "")
        if bbox is None:
            return ActionResult(success=False, action_type="click", error=f"Ref {action.target_uid} not found.")
        cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
        await _pw_call(self._page.mouse.click(cx, cy, delay=50), "mouse.click")
        return ActionResult(success=True, action_type="click", target_uid=action.target_uid, message=f"Physical click at [{cx}, {cy}]")

    async def _do_type(self, action: ActionRequest) -> ActionResult:
        bbox = self._ref_counter.get_bbox_for_ref(action.target_uid or "")
        if bbox is None:
            return ActionResult(success=False, action_type="type", error=f"Ref {action.target_uid} not found.")
        cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
        await _pw_call(self._page.mouse.click(cx, cy), "mouse.click(type)")
        await asyncio.sleep(0.1)
        await _pw_call(self._page.keyboard.down("Control"), "keyboard.down(Ctrl)")
        await _pw_call(self._page.keyboard.press("a"), "keyboard.press(a)")
        await _pw_call(self._page.keyboard.up("Control"), "keyboard.up(Ctrl)")
        await _pw_call(self._page.keyboard.press("Backspace"), "keyboard.press(BS)")
        await _pw_call(self._page.keyboard.type(action.value or "", delay=20), "keyboard.type")
        return ActionResult(success=True, action_type="type", target_uid=action.target_uid)

    async def _do_press_key(self, action: ActionRequest) -> ActionResult:
        await _pw_call(self._page.keyboard.press(action.value or "Enter"), "keyboard.press")
        return ActionResult(success=True, action_type="press_key")

    async def _do_scroll(self, action: ActionRequest) -> ActionResult:
        delta = 500 if (action.value or "down") == "down" else -500
        await _pw_call(self._page.mouse.wheel(0, delta), "mouse.wheel")
        try:
            await self._page.evaluate("""
                new Promise(resolve => {
                    let count = 0;
                    function check() {
                        count++;
                        if (count >= 3) { resolve(true); return; }
                        requestAnimationFrame(check);
                    }
                    requestAnimationFrame(check);
                    setTimeout(() => resolve(true), 200);
                })
            """)
        except Exception:
            await asyncio.sleep(0.1)
        return ActionResult(success=True, action_type="scroll")

    async def _do_wait(self, action: ActionRequest) -> ActionResult:
        duration = float(action.value or 1.0)
        await asyncio.sleep(duration)
        return ActionResult(success=True, action_type="wait")

    async def _do_execute_js(self, action: ActionRequest) -> ActionResult:
        code = action.value or ""
        # Block navigation via JS — agents must use perceive→click, not JS hacks.
        _NAV_PATTERNS = ("window.location", "document.location", "window.open(", "location.href", "location.replace(")
        code_lower = code.lower().strip()
        for pat in _NAV_PATTERNS:
            if pat in code_lower:
                return ActionResult(
                    success=False, action_type="execute_js",
                    error=f"Navigation via execute_js is blocked ({pat}). Use perceive to find the element, then click it by UID.",
                )
        res = await self._page.evaluate(code)
        return ActionResult(success=True, action_type="execute_js", message=str(res))

    async def _do_sequence(self, action: ActionRequest) -> ActionResult:
        """Execute a list of sub-actions atomically in a single round-trip."""
        import json as _json
        try:
            sub_actions_raw = _json.loads(action.value or "[]")
        except (ValueError, TypeError) as exc:
            return ActionResult(
                success=False, action_type="sequence",
                error=f"Invalid sequence value (must be JSON array): {exc}",
            )
        if not isinstance(sub_actions_raw, list) or len(sub_actions_raw) == 0:
            return ActionResult(
                success=False, action_type="sequence",
                error="Sequence value must be a non-empty JSON array.",
            )
        results: list[ActionResult] = []
        for i, sub_raw in enumerate(sub_actions_raw):
            sub_raw.setdefault("session_id", action.session_id)
            sub_raw.setdefault("context_env", action.context_env)
            try:
                sub_action = ActionRequest(**sub_raw)
            except Exception as exc:
                return ActionResult(
                    success=False, action_type="sequence",
                    error=f"Sub-action [{i}] invalid: {exc}",
                )
            if sub_action.action_type == "sequence":
                return ActionResult(
                    success=False, action_type="sequence",
                    error="Nested sequences are not allowed.",
                )
            handler = self._ACTION_MAP.get(sub_action.action_type)
            if not handler:
                return ActionResult(
                    success=False, action_type="sequence",
                    error=f"Sub-action [{i}] unknown type: {sub_action.action_type}",
                )
            sub_result = await handler(self, sub_action)
            results.append(sub_result)
            if not sub_result.success:
                return ActionResult(
                    success=False, action_type="sequence",
                    error=f"Sub-action [{i}] ({sub_action.action_type}) failed: {sub_result.error}",
                    message=f"Aborted at step {i}/{len(sub_actions_raw)}",
                )
        return ActionResult(
            success=True, action_type="sequence",
            message=f"All {len(results)} sub-actions completed.",
        )

    # ------------------------------------------------------------------
    # Quick perceive (lightweight, no DOM extraction)
    # ------------------------------------------------------------------

    async def perceive_quick(self) -> ContextPerception:
        """Lightweight perceive: URL + title + last element count. ~50ms."""
        try:
            title = await _pw_call(self._page.title(), "page.title")
        except Exception:
            title = ""
        url = self._page.url
        element_count = self._ref_counter._counter
        return ContextPerception(
            state=TaskState.IDLE,
            session_id=self._session_id,
            active_window_title=title,
            context_env="web",
            current_url=url,
            elements=[],
            spatial_context=f"[quick] Last element count: {element_count}",
        )

    _ACTION_MAP = {
        "click": _do_click,
        "type": _do_type,
        "press_key": _do_press_key,
        "scroll": _do_scroll,
        "wait": _do_wait,
        "execute_js": _do_execute_js,
        "sequence": _do_sequence,
    }
