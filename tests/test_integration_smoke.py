"""Smoke tests for the perception fusion integration.

These tests verify the imports and wiring work correctly without
requiring a live Windows desktop.
"""
import pytest
import os


def test_fusion_module_importable():
    """perception_fusion.py can be imported."""
    from bridges.desktop_bridge.perception_fusion import PerceptionFusion, visual_gatekeeper_check
    assert PerceptionFusion is not None
    assert visual_gatekeeper_check is not None


def test_fusion_merge_returns_list():
    from bridges.desktop_bridge.perception_fusion import PerceptionFusion
    from core.models.schemas import UIDNode

    f = PerceptionFusion()
    result = f.merge([], [])
    assert isinstance(result, list)


def test_feature_flag_disables_fusion():
    """OPENCLAW_PERCEPTION_FUSION=0 makes merge() just concatenate."""
    os.environ["OPENCLAW_PERCEPTION_FUSION"] = "0"
    try:
        import importlib
        import bridges.desktop_bridge.perception_fusion as pf
        importlib.reload(pf)

        from core.models.schemas import UIDNode
        oc = UIDNode(uid="oc_0", tag="section", role="section", text="container",
                     bbox=(0, 0, 400, 300), interactable=False, attributes={})
        vc = UIDNode(uid="vc_0", tag="button", role="button", text="btn",
                     bbox=(50, 50, 80, 30), interactable=True,
                     attributes={"confidence": "0.9"})

        f = pf.PerceptionFusion()
        result = f.merge([oc], [vc])
        # With fusion disabled, should just concatenate — both present, no fusion logic.
        assert len(result) == 2
        uids = {n.uid for n in result}
        assert uids == {"oc_0", "vc_0"}
    finally:
        os.environ["OPENCLAW_PERCEPTION_FUSION"] = "1"
        import importlib
        import bridges.desktop_bridge.perception_fusion as pf
        importlib.reload(pf)


def test_lookup_accepts_crop_bytes_kwarg():
    """knowledge_base.lookup() accepts optional crop_bytes."""
    from memory_core.knowledge_base import lookup
    import inspect
    sig = inspect.signature(lookup)
    assert "crop_bytes" in sig.parameters


def test_uia_extractor_has_interactable_tags():
    """Verify extraction constants exist."""
    from bridges.desktop_bridge.uia_extractor import _INTERACTABLE_TAGS
    assert "button" in _INTERACTABLE_TAGS
    assert "input" in _INTERACTABLE_TAGS
