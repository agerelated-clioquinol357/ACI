"""
OpenClaw 2.0 ACI Framework - Fast OCR Detection Tier.

Primary: Windows built-in OCR (via PowerShell WinRT bridge) — ~200ms GPU-accelerated.
Fallback: Tesseract (pytesseract) — ~300ms cross-platform.

The PowerShell approach works on ANY Python version (no winrt package needed)
by invoking Windows.Media.Ocr through a small PowerShell script that returns
JSON word-level results.

This tier runs OCR on the full screenshot, groups words into phrases,
and fuses results with cursor probe elements to assign text labels.
Elements with cursor hits but no OCR overlap are flagged ``needs_contour``.
"""

from __future__ import annotations

import io
import json
import logging
import os
import platform
import subprocess
import tempfile
from typing import Any, Optional

from core.detection_tier import DetectedElement, DetectionTier, TierResult

logger = logging.getLogger(__name__)

_IS_WINDOWS = platform.system() == "Windows"

# ---------------------------------------------------------------------------
# PIL
# ---------------------------------------------------------------------------

try:
    from PIL import Image
    _PIL_AVAILABLE = True
except ImportError:
    Image = None  # type: ignore[assignment, misc]
    _PIL_AVAILABLE = False

# ---------------------------------------------------------------------------
# Tesseract availability
# ---------------------------------------------------------------------------

_TESSERACT_AVAILABLE = False

try:
    import pytesseract  # type: ignore[import-untyped]
    # Verify binary exists.
    pytesseract.get_tesseract_version()
    _TESSERACT_AVAILABLE = True
    logger.info("fast_ocr: Tesseract available (binary found).")
except Exception:
    try:
        import pytesseract  # type: ignore[import-untyped]
        # Package installed but binary missing — check common paths.
        for path in [
            r"C:\Program Files\Tesseract-OCR\tesseract.exe",
            r"C:\Program Files (x86)\Tesseract-OCR\tesseract.exe",
        ]:
            if os.path.isfile(path):
                pytesseract.pytesseract.tesseract_cmd = path
                _TESSERACT_AVAILABLE = True
                logger.info("fast_ocr: Tesseract found at %s", path)
                break
        if not _TESSERACT_AVAILABLE:
            logger.info("fast_ocr: pytesseract installed but Tesseract binary not found.")
    except ImportError:
        logger.info("fast_ocr: pytesseract not available.")


# ---------------------------------------------------------------------------
# OCR word result
# ---------------------------------------------------------------------------

class _OcrWord:
    __slots__ = ("text", "x", "y", "w", "h")

    def __init__(self, text: str, x: int, y: int, w: int, h: int) -> None:
        self.text = text
        self.x = x
        self.y = y
        self.w = w
        self.h = h


# ---------------------------------------------------------------------------
# Windows OCR via PowerShell (works on any Python version)
# ---------------------------------------------------------------------------

# PowerShell script that calls Windows.Media.Ocr and returns JSON.
_PS_OCR_SCRIPT = r'''param([string]$ImagePath, [string]$OutputPath)
Add-Type -AssemblyName System.Runtime.WindowsRuntime

$asTaskGeneric = ([System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' })[0]
Function Await($WinRtTask, $ResultType) {
    $asTask = $asTaskGeneric.MakeGenericMethod($ResultType)
    $netTask = $asTask.Invoke($null, @($WinRtTask))
    $netTask.Wait(-1) | Out-Null
    $netTask.Result
}

[Windows.Graphics.Imaging.BitmapDecoder, Windows.Graphics.Imaging, ContentType = WindowsRuntime] | Out-Null
[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null
[Windows.Storage.StorageFile, Windows.Storage, ContentType = WindowsRuntime] | Out-Null

$storageFile = Await ([Windows.Storage.StorageFile]::GetFileFromPathAsync($ImagePath)) ([Windows.Storage.StorageFile])
$stream = Await ($storageFile.OpenAsync([Windows.Storage.FileAccessMode]::Read)) ([Windows.Storage.Streams.IRandomAccessStream])
$decoder = Await ([Windows.Graphics.Imaging.BitmapDecoder]::CreateAsync($stream)) ([Windows.Graphics.Imaging.BitmapDecoder])
$bitmap = Await ($decoder.GetSoftwareBitmapAsync()) ([Windows.Graphics.Imaging.SoftwareBitmap])

$engine = [Windows.Media.Ocr.OcrEngine]::TryCreateFromUserProfileLanguages()
if ($null -eq $engine) { exit 1 }

$result = Await ($engine.RecognizeAsync($bitmap)) ([Windows.Media.Ocr.OcrResult])

$words = @()
foreach ($line in $result.Lines) {
    foreach ($word in $line.Words) {
        $r = $word.BoundingRect
        $words += @{
            text = $word.Text
            x = [int]$r.X
            y = [int]$r.Y
            w = [int]$r.Width
            h = [int]$r.Height
        }
    }
}

$stream.Dispose()
$json = ConvertTo-Json $words -Compress
[System.IO.File]::WriteAllText($OutputPath, $json, [System.Text.Encoding]::UTF8)
'''

_WIN_OCR_AVAILABLE = False
if _IS_WINDOWS:
    # Test if Windows OCR is available (Windows 10+).
    try:
        result = subprocess.run(
            ["powershell", "-Command",
             "[Windows.Media.Ocr.OcrEngine, Windows.Media.Ocr, ContentType = WindowsRuntime] | Out-Null; Write-Output 'OK'"],
            capture_output=True, encoding="utf-8", errors="replace", timeout=10,
        )
        if "OK" in result.stdout:
            _WIN_OCR_AVAILABLE = True
            logger.info("fast_ocr: Windows OCR API available (via PowerShell).")
        else:
            logger.info("fast_ocr: Windows OCR API not available on this system.")
    except Exception:
        logger.info("fast_ocr: Could not verify Windows OCR availability.")


def _run_windows_ocr(image_bytes: bytes) -> list[_OcrWord]:
    """Run Windows OCR via PowerShell and return word-level results."""
    if not _WIN_OCR_AVAILABLE:
        return []

    tmp_img = None
    tmp_ps = None
    tmp_out = None
    try:
        # Write image to temp file.
        tmp_img = tempfile.NamedTemporaryFile(suffix=".png", delete=False)
        tmp_img.write(image_bytes)
        tmp_img.close()

        # Output JSON file.
        tmp_out = tempfile.NamedTemporaryFile(suffix=".json", delete=False)
        tmp_out.close()

        # Write PowerShell script to temp file.
        tmp_ps = tempfile.NamedTemporaryFile(suffix=".ps1", delete=False, mode="w", encoding="utf-8")
        tmp_ps.write(_PS_OCR_SCRIPT)
        tmp_ps.close()

        result = subprocess.run(
            ["powershell", "-ExecutionPolicy", "Bypass", "-NoProfile",
             "-File", tmp_ps.name, tmp_img.name, tmp_out.name],
            capture_output=True, timeout=15,
        )

        if result.returncode != 0:
            stderr = result.stderr.decode("utf-8", errors="replace")
            logger.warning("fast_ocr: PowerShell OCR failed: %s", stderr[:200])
            return []

        # Read JSON output from file (guarantees UTF-8).
        output = open(tmp_out.name, "r", encoding="utf-8-sig").read().strip()
        if not output:
            return []

        data = json.loads(output)
        if not isinstance(data, list):
            data = [data]  # single word case

        words: list[_OcrWord] = []
        for item in data:
            text = item.get("text", "").strip()
            if text:
                words.append(_OcrWord(
                    text=text,
                    x=int(item.get("x", 0)),
                    y=int(item.get("y", 0)),
                    w=int(item.get("w", 0)),
                    h=int(item.get("h", 0)),
                ))
        return words

    except json.JSONDecodeError as exc:
        logger.warning("fast_ocr: Windows OCR JSON parse error: %s", exc)
        return []
    except subprocess.TimeoutExpired:
        logger.warning("fast_ocr: Windows OCR timed out.")
        return []
    except Exception as exc:
        logger.warning("fast_ocr: Windows OCR error: %s", exc)
        return []
    finally:
        for tmp in (tmp_img, tmp_ps, tmp_out):
            if tmp:
                try:
                    os.unlink(tmp.name)
                except OSError:
                    pass


# ---------------------------------------------------------------------------
# Tesseract implementation
# ---------------------------------------------------------------------------

def _run_tesseract_ocr(image_bytes: bytes) -> list[_OcrWord]:
    """Run Tesseract OCR and return word-level results."""
    if not _TESSERACT_AVAILABLE or not _PIL_AVAILABLE:
        return []

    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        data = pytesseract.image_to_data(img, output_type=pytesseract.Output.DICT)

        words: list[_OcrWord] = []
        n_boxes = len(data["text"])
        for i in range(n_boxes):
            text = data["text"][i].strip()
            conf = int(data["conf"][i])
            if text and conf > 30:
                words.append(_OcrWord(
                    text=text,
                    x=int(data["left"][i]),
                    y=int(data["top"][i]),
                    w=int(data["width"][i]),
                    h=int(data["height"][i]),
                ))

        return words

    except Exception as exc:
        logger.warning("fast_ocr: Tesseract OCR failed: %s", exc)
        return []


# ---------------------------------------------------------------------------
# Word grouping — merge adjacent words into phrases
# ---------------------------------------------------------------------------

def _group_words(
    words: list[_OcrWord],
    gap_px: int = 10,
) -> list[_OcrWord]:
    """Group horizontally adjacent words on the same line into phrases."""
    if not words:
        return []

    sorted_words = sorted(words, key=lambda w: (w.y, w.x))

    groups: list[_OcrWord] = []
    current: Optional[_OcrWord] = None

    for word in sorted_words:
        if current is None:
            current = _OcrWord(word.text, word.x, word.y, word.w, word.h)
            continue

        overlap_y = min(current.y + current.h, word.y + word.h) - max(current.y, word.y)
        min_h = min(current.h, word.h)
        same_line = min_h > 0 and overlap_y / min_h > 0.5

        horiz_gap = word.x - (current.x + current.w)
        close = horiz_gap <= gap_px

        if same_line and close:
            new_x = min(current.x, word.x)
            new_y = min(current.y, word.y)
            new_right = max(current.x + current.w, word.x + word.w)
            new_bottom = max(current.y + current.h, word.y + word.h)
            current = _OcrWord(
                text=current.text + " " + word.text,
                x=new_x, y=new_y,
                w=new_right - new_x, h=new_bottom - new_y,
            )
        else:
            groups.append(current)
            current = _OcrWord(word.text, word.x, word.y, word.w, word.h)

    if current is not None:
        groups.append(current)

    return groups


# ---------------------------------------------------------------------------
# Fusion helpers
# ---------------------------------------------------------------------------

def _bbox_overlap(a: tuple[int, int, int, int], b: tuple[int, int, int, int]) -> float:
    """Compute IoU between two (x, y, w, h) bounding boxes."""
    ax2, ay2 = a[0] + a[2], a[1] + a[3]
    bx2, by2 = b[0] + b[2], b[1] + b[3]
    ix1, iy1 = max(a[0], b[0]), max(a[1], b[1])
    ix2, iy2 = min(ax2, bx2), min(ay2, by2)
    if ix2 <= ix1 or iy2 <= iy1:
        return 0.0
    inter = (ix2 - ix1) * (iy2 - iy1)
    union = a[2] * a[3] + b[2] * b[3] - inter
    return inter / union if union > 0 else 0.0


def _bbox_contains_point(bbox: tuple[int, int, int, int], px: int, py: int) -> bool:
    return bbox[0] <= px <= bbox[0] + bbox[2] and bbox[1] <= py <= bbox[1] + bbox[3]


# ---------------------------------------------------------------------------
# Detection Tier
# ---------------------------------------------------------------------------

class FastOCR(DetectionTier):
    """Windows OCR (PowerShell) + Tesseract fallback tier."""

    def __init__(self, *, word_group_gap_px: int = 10) -> None:
        self._gap_px = word_group_gap_px

    @property
    def name(self) -> str:
        return "ocr"

    @property
    def priority(self) -> float:
        return 2.0

    def is_available(self) -> bool:
        return _WIN_OCR_AVAILABLE or _TESSERACT_AVAILABLE

    def detect(
        self,
        screenshot_bytes: bytes,
        existing_elements: list[DetectedElement],
        *,
        roi: Optional[tuple[int, int, int, int]] = None,
        context: Optional[dict[str, Any]] = None,
    ) -> TierResult:
        if not screenshot_bytes:
            return TierResult(elements=[], source_name=self.name)

        # Run OCR: try Windows API first, then Tesseract.
        words: list[_OcrWord] = []
        if _WIN_OCR_AVAILABLE:
            words = _run_windows_ocr(screenshot_bytes)
        if not words and _TESSERACT_AVAILABLE:
            words = _run_tesseract_ocr(screenshot_bytes)

        if not words:
            for elem in existing_elements:
                if not elem.label:
                    elem.needs_contour = True
            return TierResult(elements=[], source_name=self.name)

        phrases = _group_words(words, self._gap_px)

        # Fuse with cursor probe elements: assign OCR text as labels.
        labeled_cursor_elements: set[int] = set()

        for phrase in phrases:
            phrase_bbox = (phrase.x, phrase.y, phrase.w, phrase.h)
            best_idx = -1
            best_overlap = 0.0

            for i, elem in enumerate(existing_elements):
                if i in labeled_cursor_elements:
                    continue
                overlap = _bbox_overlap(phrase_bbox, elem.bbox)
                pcx = phrase.x + phrase.w // 2
                pcy = phrase.y + phrase.h // 2
                padded = (elem.bbox[0] - 20, elem.bbox[1] - 10, elem.bbox[2] + 40, elem.bbox[3] + 20)
                if overlap > best_overlap or (overlap == 0 and _bbox_contains_point(padded, pcx, pcy)):
                    if overlap > best_overlap:
                        best_overlap = overlap
                    best_idx = i

            if best_idx >= 0:
                existing_elements[best_idx].label = phrase.text
                existing_elements[best_idx].needs_contour = False
                labeled_cursor_elements.add(best_idx)

        for i, elem in enumerate(existing_elements):
            if i not in labeled_cursor_elements and not elem.label:
                elem.needs_contour = True

        # OCR-only elements (text not matched to any cursor element).
        new_elements: list[DetectedElement] = []
        for phrase in phrases:
            phrase_bbox = (phrase.x, phrase.y, phrase.w, phrase.h)
            matched = False
            for i in labeled_cursor_elements:
                overlap = _bbox_overlap(phrase_bbox, existing_elements[i].bbox)
                if overlap > 0.1:
                    matched = True
                    break
                padded = (existing_elements[i].bbox[0] - 20, existing_elements[i].bbox[1] - 10,
                          existing_elements[i].bbox[2] + 40, existing_elements[i].bbox[3] + 20)
                pcx = phrase.x + phrase.w // 2
                pcy = phrase.y + phrase.h // 2
                if _bbox_contains_point(padded, pcx, pcy):
                    matched = True
                    break

            if not matched:
                new_elements.append(DetectedElement(
                    bbox=phrase_bbox,
                    label=phrase.text,
                    tag="text",
                    interactable=False,
                    confidence=0.5,
                ))

        return TierResult(elements=new_elements, source_name=self.name)


# ---------------------------------------------------------------------------
# Public API (used by ocr_validator.py)
# ---------------------------------------------------------------------------

def ocr_full(image_bytes: bytes) -> list[dict]:
    """Run OCR on full image, return list of {text, bbox} dicts."""
    words: list[_OcrWord] = []
    if _WIN_OCR_AVAILABLE:
        words = _run_windows_ocr(image_bytes)
    if not words and _TESSERACT_AVAILABLE:
        words = _run_tesseract_ocr(image_bytes)
    return [{"text": w.text, "bbox": [w.x, w.y, w.w, w.h]} for w in words]


def ocr_crop(image_bytes: bytes, bbox: list[int]) -> Optional[str]:
    """Extract text from a bounding box region."""
    if not _PIL_AVAILABLE:
        return None
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        x, y, w, h = int(bbox[0]), int(bbox[1]), int(bbox[2]), int(bbox[3])
        x2, y2 = min(x + w, img.width), min(y + h, img.height)
        x, y = max(0, x), max(0, y)
        if x2 <= x or y2 <= y:
            return None
        crop = img.crop((x, y, x2, y2))
        buf = io.BytesIO()
        crop.save(buf, format="PNG")
        words: list[_OcrWord] = []
        if _WIN_OCR_AVAILABLE:
            words = _run_windows_ocr(buf.getvalue())
        if not words and _TESSERACT_AVAILABLE:
            words = _run_tesseract_ocr(buf.getvalue())
        if not words:
            return None
        return " ".join(w.text for w in words).strip() or None
    except Exception as exc:
        logger.debug("fast_ocr: ocr_crop failed for bbox %s: %s", bbox, exc)
        return None


# ---------------------------------------------------------------------------
# CLI test mode
# ---------------------------------------------------------------------------

def _test() -> None:
    import time

    print(f"Windows OCR available: {_WIN_OCR_AVAILABLE}")
    print(f"Tesseract available: {_TESSERACT_AVAILABLE}")

    if not _WIN_OCR_AVAILABLE and not _TESSERACT_AVAILABLE:
        print("No OCR backend available!")
        return

    try:
        from PIL import ImageGrab
    except ImportError:
        print("PIL.ImageGrab not available.")
        return

    print("\nCapturing screenshot...")
    img = ImageGrab.grab()
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    screenshot = buf.getvalue()
    print(f"Screenshot: {img.width}x{img.height} ({len(screenshot)} bytes)")

    t0 = time.perf_counter()
    words: list[_OcrWord] = []
    backend = "none"

    if _WIN_OCR_AVAILABLE:
        backend = "Windows OCR (PowerShell)"
        words = _run_windows_ocr(screenshot)

    if not words and _TESSERACT_AVAILABLE:
        backend = "Tesseract"
        words = _run_tesseract_ocr(screenshot)

    elapsed = (time.perf_counter() - t0) * 1000
    print(f"\n{backend}: {len(words)} words in {elapsed:.1f}ms")

    phrases = _group_words(words)
    print(f"Grouped into {len(phrases)} phrases:")
    for p in phrases[:30]:
        print(f"  [{p.x},{p.y} {p.w}x{p.h}] {p.text!r}")
    if len(phrases) > 30:
        print(f"  ... and {len(phrases) - 30} more.")


if __name__ == "__main__":
    _test()
