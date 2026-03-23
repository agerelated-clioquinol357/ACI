"""Tests for iframe frame manager and global ref counter."""
from __future__ import annotations
import pytest
from core.models.schemas import UIDNode


class TestRefCounter:
    def test_assign_refs_sequential(self):
        from bridges.web_bridge.frame_manager import RefCounter
        counter = RefCounter()
        nodes = [
            UIDNode(uid="_pending", tag="button", text="A", bbox=(0, 0, 10, 10), interactable=True),
            UIDNode(uid="_pending", tag="link", text="B", bbox=(10, 0, 10, 10), interactable=True),
        ]
        assigned = counter.assign_refs(nodes, frame_id="main")
        assert assigned[0].uid == "@e1"
        assert assigned[1].uid == "@e2"

    def test_cross_frame_no_collision(self):
        from bridges.web_bridge.frame_manager import RefCounter
        counter = RefCounter()
        main_nodes = [UIDNode(uid="_pending", tag="button", text="A", bbox=(0, 0, 10, 10), interactable=True)]
        iframe_nodes = [UIDNode(uid="_pending", tag="link", text="B", bbox=(10, 0, 10, 10), interactable=True)]
        main_assigned = counter.assign_refs(main_nodes, frame_id="main")
        iframe_assigned = counter.assign_refs(iframe_nodes, frame_id="iframe_0")
        assert main_assigned[0].uid == "@e1"
        assert iframe_assigned[0].uid == "@e2"

    def test_ref_to_frame_mapping(self):
        from bridges.web_bridge.frame_manager import RefCounter
        counter = RefCounter()
        nodes = [UIDNode(uid="_pending", tag="button", text="A", bbox=(0, 0, 10, 10), interactable=True)]
        counter.assign_refs(nodes, frame_id="iframe_1")
        assert counter.get_frame_for_ref("@e1") == "iframe_1"

    def test_reset_clears_state(self):
        from bridges.web_bridge.frame_manager import RefCounter
        counter = RefCounter()
        nodes = [UIDNode(uid="_pending", tag="button", text="A", bbox=(0, 0, 10, 10), interactable=True)]
        counter.assign_refs(nodes, frame_id="main")
        counter.reset()
        new_nodes = [UIDNode(uid="_pending", tag="button", text="A", bbox=(0, 0, 10, 10), interactable=True)]
        new_assigned = counter.assign_refs(new_nodes, frame_id="main")
        assert new_assigned[0].uid == "@e1"


class TestFrameManager:
    def test_frame_discovery_depth_limit(self):
        from bridges.web_bridge.frame_manager import FrameManager
        fm = FrameManager(max_depth=2)
        assert fm._max_depth == 2
