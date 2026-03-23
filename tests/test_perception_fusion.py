"""Tests for PerceptionFusion spatial operations."""

import pytest
from core.models.schemas import UIDNode


def _make_node(uid, tag="button", text="btn", bbox=(0, 0, 100, 50),
               interactable=True, attributes=None, tier=None):
    return UIDNode(
        uid=uid, tag=tag, role=tag, text=text,
        bbox=bbox, interactable=interactable,
        attributes=attributes or {}, tier=tier,
    )


class TestIsContained:
    def test_fully_inside(self):
        from bridges.desktop_bridge.perception_fusion import _is_contained
        assert _is_contained((50, 50, 30, 20), (0, 0, 200, 200)) is True

    def test_outside(self):
        from bridges.desktop_bridge.perception_fusion import _is_contained
        assert _is_contained((250, 250, 30, 20), (0, 0, 200, 200)) is False

    def test_margin_tolerance(self):
        from bridges.desktop_bridge.perception_fusion import _is_contained
        # Inner extends 3px beyond outer — within 5px margin
        assert _is_contained((198, 0, 5, 5), (0, 0, 200, 200), margin=5) is True

    def test_partial_overlap_not_contained(self):
        from bridges.desktop_bridge.perception_fusion import _is_contained
        assert _is_contained((180, 180, 50, 50), (0, 0, 200, 200)) is False


class TestIoU:
    def test_identical_boxes(self):
        from bridges.desktop_bridge.perception_fusion import _iou
        assert _iou((10, 10, 100, 50), (10, 10, 100, 50)) == pytest.approx(1.0)

    def test_no_overlap(self):
        from bridges.desktop_bridge.perception_fusion import _iou
        assert _iou((0, 0, 50, 50), (200, 200, 50, 50)) == pytest.approx(0.0)

    def test_partial_overlap(self):
        from bridges.desktop_bridge.perception_fusion import _iou
        # 50% overlap in X, full Y → IoU should be > 0 and < 1
        result = _iou((0, 0, 100, 100), (50, 0, 100, 100))
        assert 0.0 < result < 1.0

    def test_iou_boundary_below_threshold(self):
        from bridges.desktop_bridge.perception_fusion import _iou
        # Boxes with IoU just under 0.7
        result = _iou((0, 0, 100, 100), (40, 0, 100, 100))
        assert result < 0.7

    def test_zero_size_box(self):
        from bridges.desktop_bridge.perception_fusion import _iou
        assert _iou((10, 10, 0, 0), (10, 10, 100, 50)) == pytest.approx(0.0)


class TestMerge:
    def setup_method(self):
        from bridges.desktop_bridge.perception_fusion import PerceptionFusion
        self.fusion = PerceptionFusion()

    def test_empty_inputs(self):
        result = self.fusion.merge([], [])
        assert result == []

    def test_uia_only(self):
        oc = _make_node("oc_0", tag="button", bbox=(10, 10, 80, 30))
        result = self.fusion.merge([oc], [])
        assert len(result) == 1
        assert result[0].uid == "oc_0"

    def test_vision_only(self):
        vc = _make_node("vc_0", tag="button", bbox=(10, 10, 80, 30),
                        attributes={"confidence": "0.9"})
        result = self.fusion.merge([], [vc])
        assert len(result) == 1
        assert result[0].uid == "vc_0"

    def test_vc_promoted_from_container(self):
        """vc_ inside non-interactable oc_ container → vc_ promoted."""
        oc_container = _make_node("oc_0", tag="section", bbox=(0, 0, 400, 300),
                                  interactable=False)
        vc_button = _make_node("vc_0", tag="button", bbox=(50, 50, 80, 30),
                               attributes={"confidence": "0.85"})
        result = self.fusion.merge([oc_container], [vc_button])
        uids = [n.uid for n in result]
        assert "vc_0" in uids
        vc_result = [n for n in result if n.uid == "vc_0"][0]
        assert vc_result.attributes.get("parent_oc") == "oc_0"

    def test_interactable_oc_keeps_uid_on_overlap(self):
        """Interactable oc_ with high IoU overlap → keep oc_, add visual_confidence."""
        oc = _make_node("oc_0", tag="button", text="Submit",
                        bbox=(100, 100, 80, 30), interactable=True,
                        attributes={"can_invoke": "True"})
        vc = _make_node("vc_0", tag="button", text="Submit",
                        bbox=(100, 100, 80, 30),
                        attributes={"confidence": "0.92"})
        result = self.fusion.merge([oc], [vc])
        assert len(result) == 1
        assert result[0].uid == "oc_0"
        assert "visual_confidence" in result[0].attributes

    def test_password_field_forces_prefer_uia(self):
        """IsPassword oc_ → force prefer_uia=true."""
        oc = _make_node("oc_0", tag="input", bbox=(100, 100, 200, 30),
                        interactable=True,
                        attributes={"is_password": "True"})
        vc = _make_node("vc_0", tag="input", bbox=(100, 100, 200, 30),
                        attributes={"confidence": "0.9"})
        result = self.fusion.merge([oc], [vc])
        assert len(result) == 1
        assert result[0].uid == "oc_0"
        assert result[0].attributes.get("prefer_uia") == "true"

    def test_no_overlap_keeps_both(self):
        """Non-overlapping oc_ and vc_ both kept."""
        oc = _make_node("oc_0", tag="button", bbox=(10, 10, 80, 30))
        vc = _make_node("vc_0", tag="button", bbox=(500, 500, 80, 30),
                        attributes={"confidence": "0.8"})
        result = self.fusion.merge([oc], [vc])
        assert len(result) == 2
        uids = {n.uid for n in result}
        assert uids == {"oc_0", "vc_0"}

    def test_multiple_vc_inside_same_container(self):
        """All vc_ nodes inside a single oc_ container are promoted."""
        container = _make_node("oc_0", tag="section", bbox=(0, 0, 500, 400),
                               interactable=False)
        vc1 = _make_node("vc_0", tag="button", bbox=(10, 10, 60, 30),
                         attributes={"confidence": "0.8"})
        vc2 = _make_node("vc_1", tag="button", bbox=(100, 10, 60, 30),
                         attributes={"confidence": "0.75"})
        vc3 = _make_node("vc_2", tag="input", bbox=(200, 10, 120, 30),
                         attributes={"confidence": "0.9"})
        result = self.fusion.merge([container], [vc1, vc2, vc3])
        vc_uids = {n.uid for n in result if n.uid.startswith("vc_")}
        assert vc_uids == {"vc_0", "vc_1", "vc_2"}

    def test_iou_merge_field_policy(self):
        """IoU > 0.7 → merged node uses oc_ uid+tag, vc_ confidence, fused tier."""
        oc = _make_node("oc_0", tag="button", text="OK",
                        bbox=(100, 100, 80, 30), interactable=True)
        vc = _make_node("vc_0", tag="button", text="",
                        bbox=(102, 101, 78, 29),
                        attributes={"confidence": "0.88"})
        result = self.fusion.merge([oc], [vc])
        assert len(result) == 1
        merged = result[0]
        assert merged.uid == "oc_0"
        assert merged.tag == "button"
        assert merged.text == "OK"
        assert merged.tier == "fused"
        assert merged.interactable is True
