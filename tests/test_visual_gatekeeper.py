"""Tests for the Visual Gatekeeper pre-act validation."""
import pytest


@pytest.fixture
def gatekeeper():
    """Import the standalone gatekeeper function."""
    from bridges.desktop_bridge.perception_fusion import visual_gatekeeper_check
    return visual_gatekeeper_check


def test_blank_region_fails(gatekeeper):
    """Solid white region → gatekeeper rejects."""
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")

    # Create a solid white 200x100 image.
    img = np.full((100, 200, 3), 255, dtype=np.uint8)
    _, png = cv2.imencode(".png", img)

    assert gatekeeper((0, 0, 200, 100), png.tobytes()) is False


def test_button_with_text_passes(gatekeeper):
    """Image with text/edges → gatekeeper passes."""
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")

    # Create image with strong edges (black rectangle on white).
    img = np.full((100, 200, 3), 255, dtype=np.uint8)
    cv2.rectangle(img, (20, 20), (180, 80), (0, 0, 0), 2)
    cv2.putText(img, "OK", (70, 60), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 0, 0), 2)
    _, png = cv2.imencode(".png", img)

    assert gatekeeper((0, 0, 200, 100), png.tobytes()) is True


def test_solid_color_button_passes(gatekeeper):
    """Colored button (low variance but clear edges) → passes."""
    np = pytest.importorskip("numpy")
    cv2 = pytest.importorskip("cv2")

    # Blue button with border.
    img = np.full((40, 120, 3), 200, dtype=np.uint8)  # light gray bg
    cv2.rectangle(img, (5, 5), (115, 35), (255, 0, 0), -1)  # blue fill
    cv2.rectangle(img, (5, 5), (115, 35), (0, 0, 0), 1)     # black border
    _, png = cv2.imencode(".png", img)

    assert gatekeeper((0, 0, 120, 40), png.tobytes()) is True
