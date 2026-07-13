"""Extra coverage for agent.perception (internals not exercised by the lazy-vision tests)."""

from __future__ import annotations

import io
import json
import sys
import types

import pytest
from PIL import Image

from agent.perception import PerceptionModule
from mcp_client import ToolResult
from tests.fakes import FakeMCP


@pytest.fixture
def pm(config):
    module = PerceptionModule(config)
    yield module
    module.shutdown()


# ---------------------------------------------------------------------------
# _compress / hashing
# ---------------------------------------------------------------------------

def test_compress_png_and_inverse_dpi(pm, config, monkeypatch):
    """PNG format is honored; the image follows the inverse-DPI rule (at
    200% scaling a 4000x2000 shot is halved, same as OCR input)."""
    config.screenshot.format = "PNG"
    monkeypatch.setattr("agent.perception._display_scale", lambda: 2.0)

    out = pm._compress(Image.new("RGB", (4000, 2000), "red"))

    assert out[:8] == b"\x89PNG\r\n\x1a\n"
    assert Image.open(io.BytesIO(out)).size == (2000, 1000)


def test_compress_jpeg(pm, config):
    config.screenshot.format = "JPEG"
    out = pm._compress(Image.new("RGB", (40, 40), "blue"))
    assert out[:2] == b"\xff\xd8"


def test_compute_image_hash_deterministic_and_sensitive():
    def pattern(orientation: str) -> Image.Image:
        img = Image.new("L", (16, 16), 0)
        px = img.load()
        for y in range(16):
            for x in range(16):
                if (orientation == "h" and x < 8) or (orientation == "v" and y < 8):
                    px[x, y] = 255
        return img.convert("RGB")

    a = pattern("h")
    b = pattern("h")
    c = pattern("v")  # different geometry -> different average-hash

    ha = PerceptionModule._compute_image_hash(a)
    assert ha == PerceptionModule._compute_image_hash(b)
    assert ha != PerceptionModule._compute_image_hash(c)
    assert len(ha) == 64
    assert all(ch in "0123456789abcdef" for ch in ha)


def test_compute_ui_hash_changes_with_inputs():
    base = PerceptionModule._compute_ui_hash("img", "hello", {"snapshot": "x"})
    same = PerceptionModule._compute_ui_hash("img", "hello", {"snapshot": "x"})
    diff_ocr = PerceptionModule._compute_ui_hash("img", "world", {"snapshot": "x"})
    diff_tree = PerceptionModule._compute_ui_hash("img", "hello", {"snapshot": "y"})

    assert base == same
    assert base != diff_ocr
    assert base != diff_tree


# ---------------------------------------------------------------------------
# _crop_to_active_window
# ---------------------------------------------------------------------------

def _fake_win32(rect, hwnd=7):
    return types.SimpleNamespace(
        GetForegroundWindow=lambda: hwnd,
        GetWindowRect=lambda _h: rect,
    )


def test_crop_to_active_window_crops(pm, config, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", _fake_win32((10, 10, 50, 50)))
    out = pm._crop_to_active_window(Image.new("RGB", (200, 200)))
    assert out.size == (40, 40)


def test_crop_to_active_window_no_hwnd(pm, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", _fake_win32((10, 10, 50, 50), hwnd=0))
    img = Image.new("RGB", (200, 200))
    assert pm._crop_to_active_window(img).size == img.size


def test_crop_to_active_window_degenerate_rect(pm, monkeypatch):
    monkeypatch.setitem(sys.modules, "win32gui", _fake_win32((10, 10, 10, 50)))
    img = Image.new("RGB", (200, 200))
    assert pm._crop_to_active_window(img).size == img.size


# ---------------------------------------------------------------------------
# _generate_annotated
# ---------------------------------------------------------------------------

def test_generate_annotated_uses_visualizer(pm, tmp_path, monkeypatch):
    fake_visualizer = types.SimpleNamespace(
        visualize_som=lambda img, ann: Image.new("RGB", (5, 5), "green")
    )
    monkeypatch.setitem(sys.modules, "ui_detector.visualizer", fake_visualizer)

    shot = tmp_path / "shot.jpg"
    Image.new("RGB", (40, 40)).save(shot, "JPEG")

    out = PerceptionModule._generate_annotated(
        shot, [{"center_x": 0.5, "center_y": 0.5}]
    )
    assert out.size == (5, 5)


# ---------------------------------------------------------------------------
# _fetch_ui_tree
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_fetch_ui_tree_no_mcp(pm):
    pm.mcp = None
    assert await pm._fetch_ui_tree() == {}


@pytest.mark.asyncio
async def test_fetch_ui_tree_windows_box(pm):
    inner = '    desktop\n    ├── window "任务栏"\n    │   └── (728,1416) 按钮 "开始"  [action: click]'
    wrapped = json.dumps([inner])
    mcp = FakeMCP()
    mcp.set_result("windows", "Snapshot", ToolResult(success=True, content=wrapped))
    pm.mcp = mcp

    result = await pm._fetch_ui_tree()
    assert "snapshot" in result
    assert "开始" in result["snapshot"]


@pytest.mark.asyncio
async def test_fetch_ui_tree_playwright_fallback(pm):
    mcp = FakeMCP()
    mcp.set_result("windows", "Snapshot", ToolResult(success=False, content=""))
    mcp.set_result(
        "playwright",
        "browser_snapshot",
        ToolResult(success=True, content="role: button\nname: Submit\nref: e1\n"),
    )
    pm.mcp = mcp

    result = await pm._fetch_ui_tree()
    assert "snapshot" in result
    assert "Submit" in result["snapshot"]


@pytest.mark.asyncio
async def test_fetch_ui_tree_both_fail_returns_empty(pm):
    mcp = FakeMCP()
    mcp.set_result("windows", "Snapshot", ToolResult(success=False, content=""))
    mcp.set_result("playwright", "browser_snapshot", ToolResult(success=False, content=""))
    pm.mcp = mcp

    assert await pm._fetch_ui_tree() == {}


@pytest.mark.asyncio
async def test_fetch_ui_tree_exception_returns_error(pm):
    class RaisingMCP(FakeMCP):
        async def call(self, server, tool_name, arguments):
            raise RuntimeError("boom")

    pm.mcp = RaisingMCP()
    result = await pm._fetch_ui_tree()
    assert "error" in result
    assert "boom" in result["error"]


# ---------------------------------------------------------------------------
# perceive: invalid-annotation filtering + annotated output
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perceive_filters_invalid_annotations_and_writes_annotated(
    pm, config, monkeypatch
):
    class SpyDetector:
        def detect(self, image):
            return [
                {"center_x": 0.5, "center_y": 0.5, "bbox": [0.4, 0.4, 0.6, 0.6], "score": 0.9},
                {"label": 2},  # invalid: no bbox -> skipped by the fusion
            ]

    pm.detector = SpyDetector()
    pm.mcp = None
    monkeypatch.setattr(pm, "_capture_screenshot", lambda: Image.new("RGB", (100, 100)))
    monkeypatch.setattr(pm, "_compress", lambda img: b"jpeg")
    monkeypatch.setattr(
        pm, "_run_ocr_detailed", lambda img: ("text", [], img.size)
    )  # triggers compensation
    monkeypatch.setattr(
        pm, "_generate_annotated", lambda path, ann: Image.new("RGB", (10, 10))
    )

    result = await pm.perceive("do")

    assert len(result.som_annotations) == 1  # invalid annotation dropped
    assert result.annotated_screenshot_path is not None
    assert result.annotated_screenshot_path.exists()


# ---------------------------------------------------------------------------
# shutdown
# ---------------------------------------------------------------------------

def test_shutdown_releases_executor(config):
    module = PerceptionModule(config)
    module.shutdown()  # should not raise
    assert module._io_executor._shutdown is True


# ---------------------------------------------------------------------------
# auto SoM compensation for UIA-less screens
# ---------------------------------------------------------------------------

class _SpyDetector:
    def __init__(self):
        self.calls = 0

    def detect(self, image):
        self.calls += 1
        return [{"center_x": 0.5, "center_y": 0.5, "bbox": [0.4, 0.4, 0.6, 0.6], "score": 0.9}]


def _patch_capture(pm, monkeypatch, ocr_text="登录"):
    monkeypatch.setattr(pm, "_capture_screenshot", lambda: Image.new("RGB", (100, 100)))
    monkeypatch.setattr(pm, "_compress", lambda img: b"jpeg")
    monkeypatch.setattr(pm, "_run_ocr_detailed", lambda img: (ocr_text, [], img.size))
    monkeypatch.setattr(
        pm, "_generate_annotated", lambda path, ann: Image.new("RGB", (10, 10))
    )


@pytest.mark.asyncio
async def test_perceive_auto_compensates_when_uia_empty(pm, monkeypatch):
    """Empty UI tree + OCR text -> run YOLO detection automatically."""
    detector = _SpyDetector()
    pm.detector = detector
    pm.mcp = None  # ui_tree will be {}
    _patch_capture(pm, monkeypatch)

    result = await pm.perceive("do")

    assert detector.calls == 1
    assert len(result.som_annotations) == 1
    assert result.annotated_screenshot_path is not None


@pytest.mark.asyncio
async def test_perceive_no_compensation_when_tree_present(pm, monkeypatch):
    detector = _SpyDetector()
    pm.detector = detector
    _patch_capture(pm, monkeypatch)
    monkeypatch.setattr(pm, "_fetch_ui_tree", lambda: _async_return({"snapshot": "buttons"}))

    result = await pm.perceive("do")

    assert detector.calls == 0
    assert result.som_annotations == []


@pytest.mark.asyncio
async def test_perceive_no_compensation_when_ocr_empty(pm, monkeypatch):
    detector = _SpyDetector()
    pm.detector = detector
    pm.mcp = None
    _patch_capture(pm, monkeypatch, ocr_text="   ")

    result = await pm.perceive("do")

    assert detector.calls == 0


@pytest.mark.asyncio
async def test_perceive_compensation_disabled_by_config(pm, config, monkeypatch):
    config.yolo.auto_compensate = False
    detector = _SpyDetector()
    pm.detector = detector
    pm.mcp = None
    _patch_capture(pm, monkeypatch)

    result = await pm.perceive("do")

    assert detector.calls == 0


async def _async_return(value):
    return value


# ---------------------------------------------------------------------------
# Coordinate-space contract: the model sees the compressed image and gives
# coordinates in ITS space; the orchestrator rescales to native pixels.
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_perceive_records_compressed_dimensions(pm, monkeypatch):
    """Perception must record both native and compressed (model-visible) sizes."""
    monkeypatch.setattr(
        pm, "_capture_screenshot", lambda: Image.new("RGB", (2560, 1440))
    )
    monkeypatch.setattr(pm, "_run_ocr_detailed", lambda img: ("", [], img.size))
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)
    pm.mcp = None  # real _compress runs: 2560x1440 at 125% -> 2048x1152

    p = await pm.perceive("x")

    assert (p.screen_width, p.screen_height) == (2560, 1440)
    assert (p.screenshot_width, p.screenshot_height) == (2048, 1152)


@pytest.mark.asyncio
async def test_description_declares_coordinate_space(pm, monkeypatch):
    """The description tells the model to give loc in the screenshot's space."""
    monkeypatch.setattr(
        pm, "_capture_screenshot", lambda: Image.new("RGB", (2560, 1440))
    )
    monkeypatch.setattr(pm, "_run_ocr_detailed", lambda img: ("", [], img.size))
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.0)
    pm.mcp = None

    p = await pm.perceive("x")

    assert "2560x1440" in p.description
    assert "loc" in p.description
