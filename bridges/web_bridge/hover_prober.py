"""
OpenClaw 2.0 ACI Framework - Proactive Hover Prober.
Identifies trigger elements and hovers to discover hidden menu items.
"""
from __future__ import annotations
import asyncio
import logging
import os
import time
from typing import Callable, Awaitable
from core.models.schemas import UIDNode

logger = logging.getLogger(__name__)
_HOVER_ENABLED = os.environ.get("OPENCLAW_WEB_HOVER_PROBE", "1") != "0"
_MAX_PROBES = int(os.environ.get("OPENCLAW_WEB_HOVER_MAX_PROBES", "5"))
_MAX_HOVER_ELEMENTS = 50
_PROBE_WAIT_MS = 150
_TOTAL_BUDGET_S = 1.0


def _is_trigger(node: UIDNode) -> bool:
    attrs = node.attributes or {}
    if attrs.get("haspopup") in ("true", "menu", "listbox", "tree", "grid", "dialog"):
        return True
    if attrs.get("expanded") == "false":
        return True
    if node.role in ("menubar", "navigation"):
        return True
    return False


class HoverProber:
    def __init__(self, max_probes: int = _MAX_PROBES, max_elements: int = _MAX_HOVER_ELEMENTS) -> None:
        self._max_probes = max_probes
        self._max_elements = max_elements

    async def probe(self, page, trigger_candidates: list[UIDNode],
                    extract_fn: Callable[[], Awaitable[list[UIDNode]]]) -> list[UIDNode]:
        if not _HOVER_ENABLED:
            return []
        triggers = trigger_candidates[:self._max_probes]
        revealed: list[UIDNode] = []
        start = time.monotonic()
        try:
            vp = page.viewport_size
            vw = vp.get("width", 1280) if vp else 1280
            vh = vp.get("height", 900) if vp else 900
        except Exception:
            vw, vh = 1280, 900
        center_x, center_y = vw // 2, vh // 2

        for trigger in triggers:
            if time.monotonic() - start > _TOTAL_BUDGET_S:
                break
            if len(revealed) >= self._max_elements:
                break
            if not trigger.bbox:
                continue
            cx = trigger.bbox[0] + trigger.bbox[2] // 2
            cy = trigger.bbox[1] + trigger.bbox[3] // 2
            try:
                await page.mouse.move(cx, cy)
                await asyncio.sleep(_PROBE_WAIT_MS / 1000)
                new_nodes = await extract_fn()
                for n in new_nodes:
                    if len(revealed) >= self._max_elements:
                        break
                    n.attributes["hover_revealed"] = "true"
                    revealed.append(n)
                await page.mouse.move(center_x, center_y)
            except Exception as exc:
                logger.debug("HoverProber: probe failed for %s: %s", trigger.uid, exc)
                continue
        logger.info("HoverProber: revealed %d elements from %d probes", len(revealed), len(triggers))
        return revealed
