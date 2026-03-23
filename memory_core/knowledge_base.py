"""
OpenClaw 2.0 ACI Framework - YAML App Knowledge Base.

Provides persistent, community-shareable storage for UI element patterns
learned from VLM identification and manual annotation.

Structure:
    data/knowledge_base/
        _common.yaml       — cross-app shortcuts and icon patterns
        chrome.yaml         — Chrome-specific elements (auto-populated)
        vscode.yaml         — VS Code elements (auto-populated)
        ...

Each per-app YAML stores elements by region_hash (normalized bbox → hash),
so we can look up a cached label without calling the VLM again.
"""

from __future__ import annotations

import difflib
import hashlib
import logging
import os
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Optional dependency
# ---------------------------------------------------------------------------

try:
    import yaml
    _YAML_AVAILABLE = True
except ImportError:
    yaml = None  # type: ignore[assignment]
    _YAML_AVAILABLE = False
    logger.info(
        "knowledge_base: PyYAML not available. "
        "Install with: pip install pyyaml"
    )

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

_DEFAULT_KB_DIR = Path(__file__).resolve().parent.parent / "data" / "knowledge_base"


def _kb_dir() -> Path:
    """Resolve knowledge base directory from env var or default."""
    custom = os.environ.get("OPENCLAW_KB_DIR")
    if custom:
        p = Path(custom)
        if not p.is_absolute():
            p = Path(__file__).resolve().parent.parent / p
        return p
    return _DEFAULT_KB_DIR


# ---------------------------------------------------------------------------
# Load / Save
# ---------------------------------------------------------------------------

def _resolve_app_name(app_name: str) -> str:
    """Resolve *app_name* to the canonical filename stem.

    Resolution order:
    1. Exact filename match (``<name>.yaml`` exists).
    2. Alias lookup from ``_alias_to_app``.
    3. Fuzzy match via ``difflib.get_close_matches`` (cutoff=0.6).
    4. Falls back to the sanitized input unchanged.
    """
    _ensure_cache()

    safe = _sanitize_name(app_name)
    kb = _kb_dir()

    # 1. Exact file match.
    if (kb / f"{safe}.yaml").exists():
        return safe

    # 2. Alias lookup.
    lower = app_name.lower()
    canonical = _alias_to_app.get(lower)
    if canonical:
        resolved = _sanitize_name(canonical)
        if (kb / f"{resolved}.yaml").exists():
            return resolved

    # 3. Fuzzy match against all known names + aliases.
    if _all_app_names:
        matches = difflib.get_close_matches(lower, _all_app_names, n=1, cutoff=0.6)
        if matches:
            best = matches[0]
            # best may be an alias — resolve to canonical app name.
            canonical = _alias_to_app.get(best, best)
            resolved = _sanitize_name(canonical)
            if (kb / f"{resolved}.yaml").exists():
                logger.debug("knowledge_base: fuzzy resolved '%s' → '%s'", app_name, resolved)
                return resolved

    # 4. Fall through — return sanitized original (may not exist).
    return safe


def load(app_name: str) -> dict:
    """Load app-specific YAML merged with _common.yaml.

    Supports exact names, aliases, and fuzzy matching.
    Returns combined dict with keys: shortcuts, common_icons, elements, etc.
    """
    if not _YAML_AVAILABLE:
        return {}

    kb = _kb_dir()
    result: dict[str, Any] = {}

    # Load common first.
    common_path = kb / "_common.yaml"
    if common_path.exists():
        try:
            data = yaml.safe_load(common_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                result.update(data)
        except Exception as exc:
            logger.warning("knowledge_base: Failed to load _common.yaml: %s", exc)

    # Resolve app name through alias / fuzzy matching.
    safe_name = _resolve_app_name(app_name)
    app_path = kb / f"{safe_name}.yaml"
    if app_path.exists():
        try:
            data = yaml.safe_load(app_path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                # Merge elements list.
                if "elements" in data:
                    result.setdefault("elements", [])
                    result["elements"].extend(data["elements"])
                # Merge other keys.
                for k, v in data.items():
                    if k != "elements":
                        result[k] = v
        except Exception as exc:
            logger.warning("knowledge_base: Failed to load %s.yaml: %s", safe_name, exc)

    return result


def lookup(app_name: str, rh: str, crop_bytes: Optional[bytes] = None) -> Optional[str]:
    """Look up a cached element label by region hash, with template match fallback.

    Args:
        app_name: Application name (e.g. "chrome").
        rh: Region hash from ``region_hash()``.
        crop_bytes: Optional crop image bytes for template matching fallback.

    Returns:
        Cached label string, or None if not found.
    """
    if not _YAML_AVAILABLE:
        return None

    kb = _kb_dir()
    safe_name = _sanitize_name(app_name)
    app_path = kb / f"{safe_name}.yaml"

    if not app_path.exists():
        return None

    try:
        data = yaml.safe_load(app_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return None

        # 1. Exact hash match (existing logic).
        for elem in data.get("elements", []):
            if elem.get("region_hash") == rh:
                return elem.get("label")

        # 2. Template match fallback (NEW).
        _t2_enabled = os.environ.get("OPENCLAW_T2_TEMPLATE_MATCH", "1") != "0"
        if crop_bytes and _t2_enabled:
            try:
                import cv2
                import numpy as np

                templates_dir = kb / "templates"
                if not templates_dir.is_dir():
                    return None

                crop_img = cv2.imdecode(
                    np.frombuffer(crop_bytes, np.uint8), cv2.IMREAD_GRAYSCALE,
                )
                if crop_img is None:
                    return None

                for elem in data.get("elements", []):
                    template_file = elem.get("icon_template")
                    if not template_file:
                        continue
                    template_path = templates_dir / template_file
                    if not template_path.exists():
                        continue

                    template_img = cv2.imread(
                        str(template_path), cv2.IMREAD_GRAYSCALE,
                    )
                    if template_img is None:
                        continue
                    if (template_img.shape[0] > crop_img.shape[0]
                            or template_img.shape[1] > crop_img.shape[1]):
                        continue

                    match = cv2.matchTemplate(
                        crop_img, template_img, cv2.TM_CCOEFF_NORMED,
                    )
                    if match.max() >= 0.85:
                        return elem.get("label")
            except Exception:
                pass  # Fallback to hash-only (existing behavior)

    except Exception:
        pass

    return None


def save_element(
    app_name: str,
    rh: str,
    label: str,
    crop_bytes: Optional[bytes] = None,
    *,
    tag: str = "button",
    bbox_relative: Optional[list[float]] = None,
) -> None:
    """Save a VLM-identified element to the app's YAML knowledge base.

    Args:
        app_name: Application name.
        rh: Region hash.
        label: Human-readable label.
        crop_bytes: Optional icon crop PNG bytes (saved as separate file).
        tag: Element type tag.
        bbox_relative: Optional normalized bbox [x_rel, y_rel, w_rel, h_rel].
    """
    if not _YAML_AVAILABLE:
        return

    kb = _kb_dir()
    kb.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_name(app_name)
    app_path = kb / f"{safe_name}.yaml"

    # Load existing data.
    data: dict = {"app": app_name, "elements": []}
    if app_path.exists():
        try:
            existing = yaml.safe_load(app_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = existing
                data.setdefault("elements", [])
        except Exception:
            pass

    # Check if region_hash already exists — update instead of duplicate.
    for elem in data["elements"]:
        if elem.get("region_hash") == rh:
            elem["label"] = label
            elem["tag"] = tag
            if bbox_relative:
                elem["bbox_relative"] = bbox_relative
            break
    else:
        entry: dict[str, Any] = {
            "region_hash": rh,
            "label": label,
            "tag": tag,
        }
        if bbox_relative:
            entry["bbox_relative"] = bbox_relative

        # Save icon crop template if provided.
        if crop_bytes:
            templates_dir = kb / "templates"
            templates_dir.mkdir(exist_ok=True)
            template_name = f"{safe_name}_{rh[:12]}.png"
            template_path = templates_dir / template_name
            template_path.write_bytes(crop_bytes)
            entry["icon_template"] = str(template_name)

        data["elements"].append(entry)

    # Write YAML.
    try:
        app_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
    except Exception as exc:
        logger.error("knowledge_base: Failed to save %s.yaml: %s", safe_name, exc)


# ---------------------------------------------------------------------------
# Region hash
# ---------------------------------------------------------------------------

def region_hash(
    bbox: tuple[int, int, int, int],
    window_size: tuple[int, int],
) -> str:
    """Compute a stable hash for a bbox region normalized to window size.

    Normalizes the bbox to relative coordinates (0.0–1.0) and rounds to
    2 decimal places for stability across minor layout shifts.

    Args:
        bbox: (x, y, w, h) in absolute pixels.
        window_size: (width, height) of the window.

    Returns:
        Hex hash string.
    """
    ww, wh = max(window_size[0], 1), max(window_size[1], 1)
    normalized = (
        round(bbox[0] / ww, 2),
        round(bbox[1] / wh, 2),
        round(bbox[2] / ww, 2),
        round(bbox[3] / wh, 2),
    )
    key = f"{normalized[0]:.2f},{normalized[1]:.2f},{normalized[2]:.2f},{normalized[3]:.2f}"
    return hashlib.md5(key.encode()).hexdigest()


# ---------------------------------------------------------------------------
# Pseudo-UIA tree persistence
# ---------------------------------------------------------------------------

def save_pseudo_uia_tree(
    app_name: str,
    nodes: list[dict],
    window_size: tuple[int, int],
    screenshot_bytes: Optional[bytes] = None,
) -> None:
    """Persist tier-detected element layout into the app YAML.

    Called after the first successful 3-tier scan of an app so subsequent
    launches can preload the known element structure without re-scanning.

    Each *node* is a dict with keys:
        uid, tag, text, bbox (x,y,w,h), interactable, tier, confidence

    **Icon-only elements** (no OCR text) are persisted with a small base64
    JPEG thumbnail crop so the agent can visually identify the element even
    when no text label is available.  Thumbnails are capped at 48×48px
    and stored as ``thumbnail_b64`` in the node entry.

    At most 60 elements are stored (text-labeled first, then icon-only).
    """
    if not _YAML_AVAILABLE or not nodes:
        return

    kb = _kb_dir()
    kb.mkdir(parents=True, exist_ok=True)

    safe_name = _sanitize_name(app_name)
    app_path = kb / f"{safe_name}.yaml"

    # Load existing data.
    data: dict = {"app": app_name, "elements": []}
    if app_path.exists():
        try:
            existing = yaml.safe_load(app_path.read_text(encoding="utf-8"))
            if isinstance(existing, dict):
                data = existing
        except Exception:
            pass

    # Optionally decode screenshot for thumbnail generation.
    _screenshot_img = None
    if screenshot_bytes:
        try:
            import numpy as np
            import cv2  # type: ignore[import]
            arr = np.frombuffer(screenshot_bytes, dtype=np.uint8)
            _screenshot_img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
        except Exception:
            pass

    ww, wh = max(window_size[0], 1), max(window_size[1], 1)

    # Pre-build a list of labeled nodes for neighbor lookup.
    labeled_nodes = [
        n for n in nodes
        if n.get("interactable")
        and n.get("bbox")
        and len(n.get("bbox", [])) >= 4
        and _is_meaningful_label(n.get("text", ""), n.get("tag", ""))
    ]

    pseudo_nodes: list[dict] = []

    for n in nodes:
        if not n.get("interactable"):
            continue
        bbox = n.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        if w <= 0 or h <= 0:
            continue

        label = n.get("text", "").strip()
        tag = n.get("tag", "button")
        has_meaningful_label = _is_meaningful_label(label, tag)

        # Compute spatial zone (window-relative position description).
        cx = x + w / 2
        cy = y + h / 2
        h_zone = "左" if cx < ww * 0.25 else ("右" if cx > ww * 0.75 else "中")
        v_zone = "上" if cy < wh * 0.2 else ("下" if cy > wh * 0.8 else "中")
        zone = f"{v_zone}{h_zone}"

        entry: dict = {
            "tag": tag,
            "tier": n.get("tier", ""),
            "confidence": round(float(n.get("confidence", 0.5)), 2),
            "zone": zone,
            "bbox_relative": [
                round(x / ww, 3),
                round(y / wh, 3),
                round(w / ww, 3),
                round(h / wh, 3),
            ],
        }

        if has_meaningful_label:
            entry["label"] = label
        else:
            entry["label"] = ""

            # Spatial hint: describe the button's position relative to nearby
            # labeled elements so the agent can infer its function even without
            # visual or textual identification.
            hint = _build_spatial_hint(
                x, y, w, h, ww, wh, zone,
                labeled_nodes,
                cursor_type=n.get("cursor_type", ""),
                tag=tag,
            )
            if hint:
                entry["spatial_hint"] = hint

            # Thumbnail for visual identification (icon-only buttons).
            if _screenshot_img is not None:
                thumb = _crop_thumbnail(_screenshot_img, x, y, w, h, max_size=48)
                if thumb:
                    entry["thumbnail_b64"] = thumb

        pseudo_nodes.append(entry)
        if len(pseudo_nodes) >= 60:
            break

    if not pseudo_nodes:
        return

    data["pseudo_uia"] = {
        "window_size": list(window_size),
        "nodes": pseudo_nodes,
        "scan_time": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
    }

    try:
        app_path.write_text(
            yaml.dump(data, allow_unicode=True, default_flow_style=False, sort_keys=False),
            encoding="utf-8",
        )
        labeled = sum(1 for n in pseudo_nodes if n.get("label"))
        icon_only = len(pseudo_nodes) - labeled
        logger.info(
            "knowledge_base: saved pseudo-UIA tree for %r "
            "(%d nodes: %d labeled, %d icon-only with thumbnail)",
            app_name, len(pseudo_nodes), labeled, icon_only,
        )
    except Exception as exc:
        logger.error("knowledge_base: failed to save pseudo-UIA for %s: %s", app_name, exc)


def _is_meaningful_label(label: str, tag: str) -> bool:
    """Return True if *label* is a real text label, not a fallback/generic one."""
    label = label.strip()
    return bool(label) and label != tag and len(label) > 1


def _build_spatial_hint(
    x: int, y: int, w: int, h: int,
    ww: int, wh: int,
    zone: str,
    labeled_nodes: list[dict],
    cursor_type: str = "",
    tag: str = "button",
) -> str:
    """Generate a natural-language spatial hint for an icon-only element.

    Combines:
    - Window-relative zone description
    - Common-pattern rules (top-right row → window controls, etc.)
    - Nearest labeled neighbor reference
    - Cursor type (IDC_HAND = clickable link-style)

    The result is stored in the pseudo-UIA YAML and injected into
    UIDNode attributes on next load, so the agent can reason about
    the element's probable function even without OCR text.
    """
    cx = x + w / 2
    cy = y + h / 2
    parts: list[str] = []

    # Zone description.
    parts.append(f"位于窗口{zone}区")

    # Common spatial patterns.
    is_small = w <= 40 and h <= 40

    if zone in ("上右",) and is_small:
        parts.append("可能是窗口控制按钮(最小化/最大化/关闭)")
    elif zone in ("上左", "上中") and is_small:
        parts.append("可能是工具栏图标或导航按钮")
    elif zone in ("下中", "下右") and tag == "button":
        parts.append("可能是提交/发送/确认按钮")
    elif zone in ("左上", "左中", "左下"):
        parts.append("可能是侧边导航图标")

    if cursor_type == "IDC_HAND":
        parts.append("鼠标悬停显示手型光标(可点击链接式)")

    # Find nearest labeled neighbor for relational context.
    nearest_label = _nearest_labeled_neighbor(x, y, w, h, labeled_nodes, max_dist=120)
    if nearest_label:
        parts.append(f"邻近元素: 「{nearest_label}」")

    return "；".join(parts) if parts else ""


def _nearest_labeled_neighbor(
    x: int, y: int, w: int, h: int,
    labeled_nodes: list[dict],
    max_dist: int = 120,
) -> Optional[str]:
    """Return the label of the closest labeled node within *max_dist* pixels."""
    cx = x + w / 2
    cy = y + h / 2
    best_dist = float("inf")
    best_label = None

    for nb in labeled_nodes:
        nb_bbox = nb.get("bbox", [])
        if len(nb_bbox) < 4:
            continue
        nx, ny, nw, nh = nb_bbox[0], nb_bbox[1], nb_bbox[2], nb_bbox[3]
        ncx = nx + nw / 2
        ncy = ny + nh / 2
        dist = ((cx - ncx) ** 2 + (cy - ncy) ** 2) ** 0.5
        if dist < best_dist and dist <= max_dist:
            nb_label = nb.get("text", "").strip()
            if nb_label and nb_label != nb.get("tag", ""):
                best_dist = dist
                best_label = nb_label[:30]

    return best_label


def _crop_thumbnail(
    img,  # numpy BGR image
    x: int, y: int, w: int, h: int,
    max_size: int = 48,
) -> Optional[str]:
    """Crop a region, resize to max_size, return base64 JPEG string or None."""
    try:
        import cv2
        import base64

        y0 = max(0, y)
        y1 = min(img.shape[0], y + h)
        x0 = max(0, x)
        x1 = min(img.shape[1], x + w)
        if y1 <= y0 or x1 <= x0:
            return None

        crop = img[y0:y1, x0:x1]
        ch, cw = crop.shape[:2]
        if ch == 0 or cw == 0:
            return None

        # Resize so largest dimension is max_size.
        scale = max_size / max(ch, cw)
        if scale < 1.0:
            new_w = max(1, int(cw * scale))
            new_h = max(1, int(ch * scale))
            crop = cv2.resize(crop, (new_w, new_h), interpolation=cv2.INTER_AREA)

        ok, buf = cv2.imencode(".jpg", crop, [cv2.IMWRITE_JPEG_QUALITY, 75])
        if not ok:
            return None
        return base64.b64encode(buf.tobytes()).decode("ascii")
    except Exception:
        return None


def load_pseudo_uia_tree(
    app_name: str,
    current_window_size: tuple[int, int],
) -> list[dict]:
    """Load previously persisted pseudo-UIA nodes for an app.

    Returns a list of dicts with reconstructed absolute bboxes based on
    the *current_window_size* (to handle window resize).  Returns []
    if no pseudo-UIA data exists or the app name can't be resolved.
    """
    if not _YAML_AVAILABLE:
        return []

    safe_name = _sanitize_name(_resolve_app_name_simple(app_name))
    kb = _kb_dir()
    app_path = kb / f"{safe_name}.yaml"

    if not app_path.exists():
        return []

    try:
        data = yaml.safe_load(app_path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return []
        pseudo = data.get("pseudo_uia")
        if not pseudo or not isinstance(pseudo, dict):
            return []

        cw, ch = max(current_window_size[0], 1), max(current_window_size[1], 1)
        result = []
        for n in pseudo.get("nodes", []):
            rel = n.get("bbox_relative")
            if not rel or len(rel) < 4:
                continue

            label = n.get("label", "").strip()
            spatial_hint = n.get("spatial_hint", "")
            thumbnail = n.get("thumbnail_b64", "")

            # For icon-only nodes: use spatial hint as the display text so
            # the agent sees something meaningful instead of an empty string.
            display_text = label if label else (spatial_hint or f"[icon@{n.get('zone', '?')}]")

            attrs: dict[str, str] = {"source": "pseudo-uia"}
            if n.get("zone"):
                attrs["zone"] = n["zone"]
            if spatial_hint and not label:
                # Attach hint to attributes too for structured access.
                attrs["spatial_hint"] = spatial_hint
            if thumbnail:
                attrs["thumbnail"] = thumbnail

            result.append({
                "uid": f"pk_{len(result)}",
                "tag": n.get("tag", "button"),
                "text": display_text,
                "tier": n.get("tier", "knowledge"),
                "confidence": n.get("confidence", 0.5),
                "interactable": True,
                "bbox": [
                    int(rel[0] * cw),
                    int(rel[1] * ch),
                    int(rel[2] * cw),
                    int(rel[3] * ch),
                ],
                "attributes": attrs,
            })
        return result

    except Exception as exc:
        logger.debug("knowledge_base: failed to load pseudo-UIA for %s: %s", app_name, exc)
        return []


def _resolve_app_name_simple(name: str) -> str:
    """Simple name resolution used internally (no side effects)."""
    _ensure_cache()
    safe = _sanitize_name(name)
    kb = _kb_dir()
    if (kb / f"{safe}.yaml").exists():
        return safe
    canonical = _alias_to_app.get(name.lower())
    if canonical:
        return _sanitize_name(canonical)
    return safe


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Reverse lookup: window_class / process_name → app name
# ---------------------------------------------------------------------------

_class_to_app: dict[str, str] = {}
_process_to_app: dict[str, str] = {}
_alias_to_app: dict[str, str] = {}
_all_app_names: list[str] = []
_cache_built: bool = False


def _ensure_cache() -> None:
    """Scan all YAML files once and build reverse-lookup maps.

    Builds: window_class→app, process_name→app, alias→app, and a flat
    list of all known names + aliases for fuzzy matching.
    """
    global _cache_built
    if _cache_built:
        return
    if not _YAML_AVAILABLE:
        _cache_built = True
        return

    kb = _kb_dir()
    if not kb.is_dir():
        _cache_built = True
        return

    names_set: set[str] = set()

    for yaml_path in kb.glob("*.yaml"):
        if yaml_path.name.startswith("_"):
            continue
        try:
            data = yaml.safe_load(yaml_path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                continue
            app_name = data.get("app", yaml_path.stem)
            wc = data.get("window_class")
            if wc:
                _class_to_app[wc] = app_name
            # Register alternative window classes (e.g. login window).
            for alt_wc in data.get("alt_window_classes", []):
                if alt_wc:
                    _class_to_app[alt_wc] = app_name
            pn = data.get("process_name")
            if pn:
                _process_to_app[pn.lower()] = app_name

            # Register alternative process names (e.g. Weixin.exe for WeChat).
            for alt_pn in data.get("alt_process_names", []):
                if alt_pn:
                    _process_to_app[alt_pn.lower()] = app_name

            # Register the canonical name and file stem.
            names_set.add(app_name.lower())
            names_set.add(yaml_path.stem.lower())

            # Register aliases.
            for alias in data.get("aliases", []):
                key = alias.lower()
                _alias_to_app[key] = app_name
                names_set.add(key)

        except Exception as exc:
            logger.debug("knowledge_base: failed to index %s: %s", yaml_path.name, exc)

    _all_app_names.extend(sorted(names_set))
    _cache_built = True
    logger.debug(
        "knowledge_base: reverse cache built — %d window_classes, %d process_names, %d aliases",
        len(_class_to_app), len(_process_to_app), len(_alias_to_app),
    )


def find_by_window_class(class_name: str) -> Optional[str]:
    """Look up app name by Win32 window class name."""
    _ensure_cache()
    return _class_to_app.get(class_name)


def find_by_process_name(process_name: str) -> Optional[str]:
    """Look up app name by process name (case-insensitive).

    Resolution order:
        1. Exact match against registered process_name fields.
        2. Stem match (strip .exe) against aliases.
        3. Substring/fuzzy match against all known names + aliases.
    """
    _ensure_cache()
    lower = process_name.lower()

    # 1. Exact match.
    result = _process_to_app.get(lower)
    if result:
        return result

    # 2. Stem match: "Weixin.exe" → "weixin" → matches alias "weixin".
    stem = lower.removesuffix(".exe").removesuffix(".app")
    result = _alias_to_app.get(stem)
    if result:
        return result

    # 3. Fuzzy match against all known names/aliases.
    if _all_app_names:
        matches = difflib.get_close_matches(stem, _all_app_names, n=1, cutoff=0.5)
        if matches:
            best = matches[0]
            canonical = _alias_to_app.get(best, best)
            kb = _kb_dir()
            resolved = _sanitize_name(canonical)
            if (kb / f"{resolved}.yaml").exists():
                logger.debug("find_by_process_name: fuzzy resolved %r → %r", process_name, resolved)
                return canonical

    return None


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _sanitize_name(name: str) -> str:
    """Sanitize app name for use as filename."""
    safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in name.lower())
    return safe or "unknown"
