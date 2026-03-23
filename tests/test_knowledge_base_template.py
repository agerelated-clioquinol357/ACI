"""Tests for knowledge_base template matching fallback."""
import pytest
import os
import tempfile
from pathlib import Path


@pytest.fixture
def tmp_kb(tmp_path, monkeypatch):
    """Create a temporary knowledge base directory."""
    monkeypatch.setenv("OPENCLAW_KB_DIR", str(tmp_path))
    # Reset the module-level cache so it picks up the new dir.
    import memory_core.knowledge_base as kb_mod
    kb_mod._cache_built = False
    kb_mod._class_to_app.clear()
    kb_mod._process_to_app.clear()
    kb_mod._alias_to_app.clear()
    kb_mod._all_app_names.clear()
    return tmp_path


def test_lookup_exact_hash_hit(tmp_kb):
    """Exact region_hash match returns label."""
    from memory_core import knowledge_base as kb
    import yaml

    app_path = tmp_kb / "testapp.yaml"
    data = {
        "app": "testapp",
        "elements": [
            {"region_hash": "abc123", "label": "Search Button", "tag": "button"}
        ]
    }
    app_path.write_text(yaml.dump(data), encoding="utf-8")

    result = kb.lookup("testapp", "abc123")
    assert result == "Search Button"


def test_lookup_hash_miss_no_template(tmp_kb):
    """Hash miss with no templates returns None."""
    from memory_core import knowledge_base as kb
    import yaml

    app_path = tmp_kb / "testapp.yaml"
    data = {"app": "testapp", "elements": [
        {"region_hash": "abc123", "label": "X", "tag": "button"}
    ]}
    app_path.write_text(yaml.dump(data), encoding="utf-8")

    result = kb.lookup("testapp", "wrong_hash")
    assert result is None


def test_lookup_backward_compat_no_crop(tmp_kb):
    """Existing callers without crop_bytes still work."""
    from memory_core import knowledge_base as kb
    import yaml

    app_path = tmp_kb / "testapp.yaml"
    data = {"app": "testapp", "elements": [
        {"region_hash": "abc123", "label": "OK", "tag": "button"}
    ]}
    app_path.write_text(yaml.dump(data), encoding="utf-8")

    # No crop_bytes argument — should still work
    result = kb.lookup("testapp", "abc123")
    assert result == "OK"


def test_lookup_template_match_fallback(tmp_kb):
    """Hash miss + matching template → returns label via OpenCV."""
    cv2 = pytest.importorskip("cv2")
    np = pytest.importorskip("numpy")
    from memory_core import knowledge_base as kb
    import yaml

    # Create a 48x48 solid gray template image.
    template_img = np.full((48, 48, 3), 128, dtype=np.uint8)
    templates_dir = tmp_kb / "templates"
    templates_dir.mkdir()
    cv2.imwrite(str(templates_dir / "testapp_abc123abc1.png"), template_img)

    app_path = tmp_kb / "testapp.yaml"
    data = {"app": "testapp", "elements": [
        {
            "region_hash": "abc123",
            "label": "Close Button",
            "tag": "button",
            "icon_template": "testapp_abc123abc1.png",
        }
    ]}
    app_path.write_text(yaml.dump(data), encoding="utf-8")

    # Create a crop image that contains the template.
    crop = np.full((60, 60, 3), 128, dtype=np.uint8)
    _, crop_bytes = cv2.imencode(".png", crop)

    result = kb.lookup("testapp", "wrong_hash", crop_bytes=crop_bytes.tobytes())
    assert result == "Close Button"
