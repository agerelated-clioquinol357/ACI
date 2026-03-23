import os
import json
import hashlib
import logging
import threading
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# Default cache directory — shared with vision_fallback.py.
_DEFAULT_CACHE_DIR = os.environ.get(
    "OPENCLAW_VISION_CACHE_DIR",
    str(Path.home() / ".openclaw" / "vision_cache"),
)


class MuscleMemoryStore:
    """Local OpenCV template matching cache.

    Maps semantic descriptions to cropped image templates.
    When a VLM (T3) identifies an element, its bounding box is cropped
    and stored here so future lookups are instant via template matching.

    Thread-safe: all index mutations are guarded by a lock.
    """

    # Maximum number of cached templates before LRU eviction.
    MAX_TEMPLATES = int(os.environ.get("OPENCLAW_MUSCLE_MAX_TEMPLATES", "200"))

    def __init__(
        self,
        cache_dir: Optional[str] = None,
        match_threshold: float = 0.85,
    ):
        self.cache_dir = Path(cache_dir or _DEFAULT_CACHE_DIR)
        self.cache_dir.mkdir(parents=True, exist_ok=True)
        self.match_threshold = match_threshold
        self._index_file = self.cache_dir / "index.json"
        self._lock = threading.Lock()
        self._index: dict[str, dict] = self._load_index()

    def _load_index(self) -> dict:
        if self._index_file.exists():
            try:
                with open(self._index_file, "r") as f:
                    return json.load(f)
            except (json.JSONDecodeError, OSError):
                return {}
        return {}

    def _save_index(self):
        with open(self._index_file, "w") as f:
            json.dump(self._index, f, indent=2)

    def _key(self, semantic_description: str) -> str:
        return hashlib.sha256(semantic_description.lower().strip().encode()).hexdigest()[:16]

    def save(
        self,
        semantic_description: str,
        cropped_image_bytes: bytes,
        app_context: str = "",
        action_type: str = "",
        action_value: str = "",
        ui_changed: Optional[bool] = None,
    ) -> str:
        """Store a cropped template image keyed by semantic description.

        Action-contextual fields (action_type, action_value, ui_changed) let
        the agent understand what this element does based on previous experience.
        E.g. "click here → search dialog opened".

        Args:
            semantic_description: Natural language label for the element.
            cropped_image_bytes: PNG bytes of the cropped element region.
            app_context: Window title / app name.
            action_type: The action that was performed (click, type, …).
            action_value: Value typed or passed to the action.
            ui_changed: Whether the UI visibly changed after the action.
        """
        key = self._key(semantic_description)
        img_path = self.cache_dir / f"{key}.png"
        img_path.write_bytes(cropped_image_bytes)
        with self._lock:
            existing = self._index.get(key, {})
            self._index[key] = {
                "description": semantic_description,
                "file": str(img_path.name),
                "match_count": existing.get("match_count", 0),
                "app_context": app_context,
                # Action context — updated every time the element is acted on.
                "last_action_type": action_type or existing.get("last_action_type", ""),
                "last_action_value": action_value or existing.get("last_action_value", ""),
                "ui_changed_after_action": (
                    ui_changed if ui_changed is not None
                    else existing.get("ui_changed_after_action")
                ),
                "use_count": existing.get("use_count", 0) + (1 if action_type else 0),
            }
            # Evict least-used templates when cache exceeds limit.
            if len(self._index) > self.MAX_TEMPLATES:
                self._evict_lru()
            self._save_index()
        logger.info(
            "Muscle memory saved: %r action=%r -> %s",
            semantic_description, action_type or "passive", img_path.name,
        )
        return key

    def get_action_context(self, semantic_description: str) -> Optional[dict]:
        """Return stored action context for an element, or None if not cached."""
        key = self._key(semantic_description)
        with self._lock:
            entry = self._index.get(key)
        if not entry:
            return None
        action_type = entry.get("last_action_type", "")
        if not action_type:
            return None
        return {
            "last_action": action_type,
            "last_value": entry.get("last_action_value", ""),
            "ui_changed": entry.get("ui_changed_after_action"),
            "use_count": entry.get("use_count", 0),
        }

    def _evict_lru(self) -> None:
        """Remove least-used templates to keep cache within MAX_TEMPLATES.

        Must be called while holding self._lock.
        """
        to_remove = len(self._index) - self.MAX_TEMPLATES
        if to_remove <= 0:
            return
        # Sort by match_count ascending (least used first).
        sorted_entries = sorted(
            self._index.items(),
            key=lambda kv: kv[1].get("match_count", 0),
        )
        for key, entry in sorted_entries[:to_remove]:
            img_path = self.cache_dir / entry.get("file", f"{key}.png")
            try:
                img_path.unlink(missing_ok=True)
            except OSError:
                pass
            del self._index[key]
        logger.info("Muscle memory evicted %d least-used templates", to_remove)

    def fast_match(self, screenshot_bytes: bytes, semantic_description: str) -> Optional[tuple[int, int]]:
        """Try to find the target in a screenshot using cached template.
        Returns (center_x, center_y) or None.
        """
        key = self._key(semantic_description)
        with self._lock:
            entry = self._index.get(key)
        if not entry:
            return None

        template_path = self.cache_dir / entry["file"]
        if not template_path.exists():
            return None

        try:
            import cv2
            import numpy as np

            # Decode screenshot
            screenshot_arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
            screenshot = cv2.imdecode(screenshot_arr, cv2.IMREAD_COLOR)
            if screenshot is None:
                return None

            # Load template
            template = cv2.imread(str(template_path), cv2.IMREAD_COLOR)
            if template is None:
                return None

            # Template matching
            result = cv2.matchTemplate(screenshot, template, cv2.TM_CCOEFF_NORMED)
            _, max_val, _, max_loc = cv2.minMaxLoc(result)

            if max_val >= self.match_threshold:
                h, w = template.shape[:2]
                center_x = max_loc[0] + w // 2
                center_y = max_loc[1] + h // 2
                with self._lock:
                    entry["match_count"] = entry.get("match_count", 0) + 1
                    self._save_index()
                logger.info(f"Muscle memory HIT: '{semantic_description}' (confidence={max_val:.3f})")
                return (center_x, center_y)

            logger.debug(f"Muscle memory MISS: '{semantic_description}' (best={max_val:.3f} < threshold={self.match_threshold})")
            return None

        except ImportError:
            logger.warning("OpenCV not installed - muscle memory disabled")
            return None
        except Exception as e:
            logger.error(f"Muscle memory match failed: {e}")
            return None

    def has_template(self, semantic_description: str) -> bool:
        key = self._key(semantic_description)
        with self._lock:
            return key in self._index

    def clear(self):
        """Clear all cached templates."""
        with self._lock:
            for f in self.cache_dir.glob("*.png"):
                f.unlink()
            self._index.clear()
            self._save_index()
        logger.info("Muscle memory cleared")

    def stats(self) -> dict:
        with self._lock:
            return {
                "total_templates": len(self._index),
                "cache_dir": str(self.cache_dir),
                "threshold": self.match_threshold,
            }
