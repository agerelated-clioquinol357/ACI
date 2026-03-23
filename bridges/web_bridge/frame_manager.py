"""
OpenClaw 2.0 ACI Framework - Frame Manager.
iframe discovery, global ref counter, ref->frame mapping.
"""
from __future__ import annotations
import logging
from typing import Optional
from core.models.schemas import UIDNode

logger = logging.getLogger(__name__)


class RefCounter:
    def __init__(self) -> None:
        self._counter: int = 0
        self._ref_to_frame: dict[str, str] = {}
        self._ref_to_bbox: dict[str, tuple[int, int, int, int]] = {}

    def reset(self) -> None:
        self._counter = 0
        self._ref_to_frame.clear()
        self._ref_to_bbox.clear()

    def assign_refs(self, nodes: list[UIDNode], frame_id: str) -> list[UIDNode]:
        result: list[UIDNode] = []
        for node in nodes:
            self._counter += 1
            ref = f"@e{self._counter}"
            updated = node.model_copy(update={"uid": ref})
            self._ref_to_frame[ref] = frame_id
            if updated.bbox:
                self._ref_to_bbox[ref] = updated.bbox
            result.append(updated)
        return result

    def get_frame_for_ref(self, ref: str) -> Optional[str]:
        return self._ref_to_frame.get(ref)

    def get_bbox_for_ref(self, ref: str) -> Optional[tuple[int, int, int, int]]:
        return self._ref_to_bbox.get(ref)

    @property
    def ref_to_frame(self) -> dict[str, str]:
        return self._ref_to_frame

    @property
    def ref_to_bbox(self) -> dict[str, tuple[int, int, int, int]]:
        return self._ref_to_bbox


class FrameManager:
    def __init__(self, max_depth: int = 2) -> None:
        self._max_depth = max_depth

    async def discover_frames(self, page) -> list[dict]:
        frames = []
        try:
            for i, frame in enumerate(page.frames):
                if frame == page.main_frame:
                    continue
                depth = self._frame_depth(frame, page.main_frame)
                if depth > self._max_depth:
                    continue
                is_same_origin = self._is_same_origin(page.url, frame.url)
                frames.append({
                    "frame": frame, "frame_id": f"iframe_{i}",
                    "is_same_origin": is_same_origin, "depth": depth, "url": frame.url,
                })
        except Exception as exc:
            logger.debug("FrameManager: frame discovery error: %s", exc)
        return frames

    @staticmethod
    def _frame_depth(frame, main_frame) -> int:
        depth = 0
        current = frame
        while current and current != main_frame:
            current = current.parent_frame
            depth += 1
        return depth

    @staticmethod
    def _is_same_origin(page_url: str, frame_url: str) -> bool:
        try:
            from urllib.parse import urlparse
            p = urlparse(page_url)
            f = urlparse(frame_url)
            return p.scheme == f.scheme and p.netloc == f.netloc
        except Exception:
            return False
