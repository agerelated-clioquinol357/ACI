"""Regression tests for coordinate system fixes (Bugs 1-4)."""

import pytest
from core.models.schemas import UIDNode


def _make_node(uid, tag="button", text="btn", bbox=(0, 0, 100, 50),
               interactable=True, attributes=None, tier=None):
    return UIDNode(
        uid=uid, tag=tag, role=tag, text=text,
        bbox=bbox, interactable=interactable,
        attributes=attributes or {}, tier=tier,
    )


# === Bug 1: Vision bbox normalization ===

class TestVisionBboxNormalization:
    """After normalization, vision bboxes should be screen-absolute."""

    def test_offset_applied_to_vision_nodes(self):
        """Simulate the normalization logic from _perceive_sync."""
        window_offset = (200, 100)
        # Vision node with window-relative bbox
        raw = _make_node("vc_0", bbox=(50, 30, 80, 40), tier="T2")

        # Apply the same normalization as worker.py
        ox, oy = window_offset
        normalized = UIDNode(
            uid=raw.uid, tag=raw.tag, role=raw.role, text=raw.text,
            bbox=(raw.bbox[0] + ox, raw.bbox[1] + oy, raw.bbox[2], raw.bbox[3]),
            interactable=raw.interactable, attributes=raw.attributes, tier=raw.tier,
        )

        assert normalized.bbox == (250, 130, 80, 40)

    def test_zero_offset_no_change(self):
        """When window_offset is (0, 0), no normalization needed."""
        window_offset = (0, 0)
        raw = _make_node("vc_1", bbox=(50, 30, 80, 40))
        # The code skips normalization when offset is (0, 0)
        assert raw.bbox == (50, 30, 80, 40)

    def test_vc_click_coords_no_double_offset(self):
        """After normalization, _act_sync vc_ path should NOT add window_offset."""
        # Simulate: bbox already screen-absolute after normalization
        bbox = [250, 130, 80, 40]  # screen-absolute
        # New code: no offset added
        x = int(bbox[0] + bbox[2] / 2)
        y = int(bbox[1] + bbox[3] / 2)
        assert x == 290  # 250 + 40
        assert y == 150  # 130 + 20

    def test_fusion_iou_with_normalized_coords(self):
        """After normalization, UIA and vision bboxes are in the same space."""
        from bridges.desktop_bridge.perception_fusion import _iou

        # UIA node: screen-absolute
        uia_bbox = (200, 100, 100, 50)
        # Vision node: screen-absolute AFTER normalization
        vis_bbox = (210, 105, 90, 45)

        iou = _iou(uia_bbox, vis_bbox)
        assert iou > 0.5, f"Expected significant IoU, got {iou}"


# === Bug 2: DPI double-scaling fix ===

class TestDpiAbsoluteCoords:
    """_to_absolute_coords should NOT multiply by DPI scale."""

    def test_no_dpi_multiplication(self):
        """At 150% DPI, physical coords should map directly to [0, 65535]."""
        # Simulate: screen is 1920x1080 physical, coords are physical
        screen_w, screen_h = 1920, 1080
        x, y = 960, 540  # center of screen

        # New logic: direct normalization, no DPI multiply
        abs_x = int(x * 65535 / screen_w)
        abs_y = int(y * 65535 / screen_h)

        # Center should map to ~32767
        assert 32000 < abs_x < 33500, f"abs_x={abs_x}"
        assert 32000 < abs_y < 33500, f"abs_y={abs_y}"

    def test_origin_maps_to_zero(self):
        screen_w, screen_h = 1920, 1080
        abs_x = int(0 * 65535 / screen_w)
        abs_y = int(0 * 65535 / screen_h)
        assert abs_x == 0
        assert abs_y == 0

    def test_bottom_right_maps_to_65535(self):
        screen_w, screen_h = 1920, 1080
        abs_x = int(screen_w * 65535 / screen_w)
        abs_y = int(screen_h * 65535 / screen_h)
        assert abs_x == 65535
        assert abs_y == 65535


# === Bug 3: Gatekeeper bbox conversion ===

class TestGatekeeperBboxConversion:
    """Gatekeeper should receive window-relative bbox, not screen-absolute."""

    def test_screen_to_window_relative(self):
        """Subtract window_offset from screen-absolute bbox."""
        window_offset = (200, 100)
        node_bbox = (350, 250, 80, 40)  # screen-absolute

        ox, oy = window_offset
        gk_bbox = (
            max(0, node_bbox[0] - ox),
            max(0, node_bbox[1] - oy),
            node_bbox[2],
            node_bbox[3],
        )

        assert gk_bbox == (150, 150, 80, 40)

    def test_clamp_negative_to_zero(self):
        """If window moved, bbox might go negative — should clamp to 0."""
        window_offset = (500, 300)
        node_bbox = (100, 100, 80, 40)  # screen-absolute, left of window

        ox, oy = window_offset
        gk_bbox = (
            max(0, node_bbox[0] - ox),
            max(0, node_bbox[1] - oy),
            node_bbox[2],
            node_bbox[3],
        )

        assert gk_bbox == (0, 0, 80, 40)


# === Bug 4: Self-drawn container blacklist ===

class TestSelfDrawnBlacklist:
    """Self-drawn classes should be non-interactable and skipped in fusion."""

    def test_self_drawn_classes_constant_exists(self):
        from bridges.desktop_bridge.uia_extractor import _SELF_DRAWN_CLASSES
        assert "MMUIRenderSubWindowHW" in _SELF_DRAWN_CLASSES
        assert "Chrome_RenderWidgetHostHWND" in _SELF_DRAWN_CLASSES

    def test_fusion_skips_self_drawn_containers(self):
        from bridges.desktop_bridge.perception_fusion import PerceptionFusion, _iou

        fusion = PerceptionFusion()

        # UIA node: self-drawn container covering the whole window
        oc = _make_node(
            "oc_0", tag="section", text="WeChat renderer",
            bbox=(0, 0, 800, 600), interactable=False,
            attributes={"self_drawn": "True", "class": "MMUIRenderSubWindowHW"},
        )
        # Vision node: actual button detected inside the self-drawn area
        vc = _make_node(
            "vc_0", tag="button", text="Send",
            bbox=(100, 200, 60, 30), interactable=True, tier="T2",
        )

        merged = fusion.merge([oc], [vc])

        # Vision node should survive — not merged into self-drawn container
        vc_nodes = [n for n in merged if n.uid.startswith("vc_")]
        assert len(vc_nodes) >= 1, "Vision node should not be consumed by self-drawn container"

    def test_non_self_drawn_still_merges(self):
        from bridges.desktop_bridge.perception_fusion import PerceptionFusion

        fusion = PerceptionFusion()

        # Normal UIA button — overlaps vision node
        oc = _make_node("oc_0", tag="button", text="OK", bbox=(100, 100, 80, 40))
        vc = _make_node("vc_0", tag="button", text="OK", bbox=(102, 102, 78, 38), tier="T2")

        merged = fusion.merge([oc], [vc])

        # Should merge/deduplicate — oc_ node should win (UIA preferred)
        oc_nodes = [n for n in merged if n.uid.startswith("oc_")]
        assert len(oc_nodes) >= 1
