"""Tests for perception fusion and UI hashing."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from agent.config import Config
from agent.perception import Perception, PerceptionModule


def _make_config(tmp_path: Path) -> Config:
    return Config(
        llm={
            "provider": "kimi",
            "base_url": "https://api.moonshot.cn/v1",
            "api_key": "test",
            "model": "kimi-k3",
            "enable_builtin_tools": False,
            "builtin_tools": [],
        },
        mcp_servers={
            "playwright": {"command": "npx", "args": [], "env": {}},
            "windows": {"command": "windows-mcp", "args": ["serve"], "env": {}},
            "filesystem": {"command": "npx", "args": [], "env": {}},
        },
        memory={"sqlite_path": str(tmp_path / "memory.db")},
        paths={
            "skills_dir": str(tmp_path / "skills"),
            "cache_dir": str(tmp_path / "cache"),
            "audit_log": str(tmp_path / "audit.log"),
        },
    )


@pytest.fixture
def config(tmp_path: Path) -> Config:
    return _make_config(tmp_path)


def test_compute_image_hash_is_deterministic(config: Config) -> None:
    module = PerceptionModule(config)
    image = Image.new("RGB", (100, 100), color="red")

    h1 = module._compute_image_hash(image)
    h2 = module._compute_image_hash(image)

    assert h1 == h2
    assert len(h1) == 64


# ---------------------------------------------------------------------------
# _run_ocr result unpacking: RapidOCR.__call__ returns (items, elapse), where
# items are [box, text, score] entries and elapse is per-stage timings. The
# timings must never leak into the OCR text the model sees.
# ---------------------------------------------------------------------------


class _FakeRapidOCR:
    """Stand-in returning the real RapidOCR (items, elapse) tuple shape."""

    def __init__(self, items, elapse=(0.05, 0.17, 0.06)):
        self._items = items
        self._elapse = elapse

    def __call__(self, path):
        return (self._items, list(self._elapse))


def test_run_ocr_extracts_every_text_line(config: Config) -> None:
    module = PerceptionModule(config)
    module._ocr = _FakeRapidOCR([
        ([[0, 0], [10, 0], [10, 10], [0, 10]], "ALPHA", 0.99),
        ([[0, 20], [10, 20], [10, 30], [0, 30]], "BRAVO", 0.95),
        ([[0, 40], [10, 40], [10, 50], [0, 50]], "CHARLIE", 0.90),
    ])

    text = module._run_ocr(Image.new("RGB", (100, 60)))

    assert text == "ALPHA\nBRAVO\nCHARLIE"


def test_run_ocr_single_line_returns_its_text(config: Config) -> None:
    module = PerceptionModule(config)
    module._ocr = _FakeRapidOCR([
        ([[0, 0], [10, 0], [10, 10], [0, 10]], "ONLY", 0.99),
    ])

    assert module._run_ocr(Image.new("RGB", (100, 60))) == "ONLY"


def test_run_ocr_empty_screen_returns_empty_text(config: Config) -> None:
    # No detections: items is None and only the elapse timings come back —
    # the OCR text must be empty, never the timing floats.
    module = PerceptionModule(config)
    module._ocr = _FakeRapidOCR(None)

    assert module._run_ocr(Image.new("RGB", (100, 60))) == ""


# ---------------------------------------------------------------------------
# _run_ocr_detailed: text + pixel boxes + scores + OCR input size. Boxes feed
# the SoM fusion (ui_detector.fusion) so OCR text becomes clickable markers.
# ---------------------------------------------------------------------------


def test_run_ocr_detailed_returns_boxes_scores_and_input_size(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.0)
    module._ocr = _FakeRapidOCR([
        ([[10, 20], [50, 20], [50, 40], [10, 40]], "ALPHA", 0.99),
        ([[5, 60], [30, 60], [30, 75], [5, 75]], "BETA", 0.88),
    ])

    text, boxes, size = module._run_ocr_detailed(Image.new("RGB", (100, 100)))

    assert text == "ALPHA\nBETA"
    assert size == (100, 100)  # 100% scaling: OCR input is the image itself
    assert len(boxes) == 2
    assert boxes[0]["bbox"] == pytest.approx([10.0, 20.0, 50.0, 40.0])
    assert boxes[0]["text"] == "ALPHA"
    assert boxes[0]["score"] == pytest.approx(0.99)
    assert boxes[1]["bbox"] == pytest.approx([5.0, 60.0, 30.0, 75.0])
    assert boxes[1]["score"] == pytest.approx(0.88)


def test_run_ocr_detailed_reports_ocr_space_when_resized(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Boxes are pixels of the RESIZED OCR input; the reported size is that
    space (not the original screenshot), so fusion normalizes correctly."""
    module = PerceptionModule(config)
    module._ocr = _FakeRapidOCR([([[0, 0], [10, 0], [10, 10], [0, 10]], "X", 0.9)])
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)

    _, boxes, size = module._run_ocr_detailed(Image.new("RGB", (2560, 1440)))

    assert size == (2048, 1152)
    assert boxes[0]["bbox"] == pytest.approx([0.0, 0.0, 10.0, 10.0])


def test_run_ocr_detailed_empty_screen_returns_empty_boxes(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.0)
    module._ocr = _FakeRapidOCR(None)

    text, boxes, size = module._run_ocr_detailed(Image.new("RGB", (100, 60)))

    assert text == ""
    assert boxes == []
    assert size == (100, 60)


def test_compute_ui_hash_changes_with_ocr(config: Config) -> None:
    module = PerceptionModule(config)
    image_hash = "img"
    tree: dict[str, Any] = {"snapshot": "tree"}

    h1 = module._compute_ui_hash(image_hash, "hello", tree)
    h2 = module._compute_ui_hash(image_hash, "world", tree)

    assert h1 != h2
    assert len(h1) == 16


def test_compute_ui_hash_changes_with_tree(config: Config) -> None:
    module = PerceptionModule(config)
    image_hash = "img"
    ocr = "text"

    h1 = module._compute_ui_hash(image_hash, ocr, {"a": 1})
    h2 = module._compute_ui_hash(image_hash, ocr, {"a": 2})

    assert h1 != h2


def test_compute_ui_hash_ignores_ocr_case_and_whitespace(config: Config) -> None:
    module = PerceptionModule(config)
    image_hash = "img"
    tree: dict[str, Any] = {}

    h1 = module._compute_ui_hash(image_hash, "  Hello World  ", tree)
    h2 = module._compute_ui_hash(image_hash, "hello world", tree)

    assert h1 == h2


def test_has_changed_detects_different_hashes(config: Config) -> None:
    module = PerceptionModule(config)
    p1 = Perception(
        screenshot_path=Path("/tmp/a.jpg"),
        description="a",
        ocr_text="a",
        ui_tree={},
        som_annotations=[],
        ui_hash="hash-a",
    )
    p2 = Perception(
        screenshot_path=Path("/tmp/b.jpg"),
        description="b",
        ocr_text="b",
        ui_tree={},
        som_annotations=[],
        ui_hash="hash-b",
    )

    assert module.has_changed(p1, p2) is True


def test_has_changed_detects_same_hash(config: Config) -> None:
    module = PerceptionModule(config)
    p1 = Perception(
        screenshot_path=Path("/tmp/a.jpg"),
        description="a",
        ocr_text="a",
        ui_tree={},
        som_annotations=[],
        ui_hash="hash-same",
    )
    p2 = Perception(
        screenshot_path=Path("/tmp/b.jpg"),
        description="b",
        ocr_text="b",
        ui_tree={},
        som_annotations=[],
        ui_hash="hash-same",
    )

    assert module.has_changed(p1, p2) is False


def test_has_changed_treats_missing_hash_as_changed(config: Config) -> None:
    module = PerceptionModule(config)
    p1 = Perception(
        screenshot_path=Path("/tmp/a.jpg"),
        description="a",
        ocr_text="a",
        ui_tree={},
        som_annotations=[],
        ui_hash="",
    )
    p2 = Perception(
        screenshot_path=Path("/tmp/b.jpg"),
        description="b",
        ocr_text="b",
        ui_tree={},
        som_annotations=[],
        ui_hash="hash",
    )

    assert module.has_changed(p1, p2) is True


def test_perception_stores_screen_dimensions() -> None:
    p = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
        screen_width=1920,
        screen_height=1080,
    )
    assert p.screen_width == 1920
    assert p.screen_height == 1080


def test_perception_stores_annotated_screenshot_path() -> None:
    p = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
        annotated_screenshot_path=Path("/tmp/test_annotated.jpg"),
    )
    assert p.annotated_screenshot_path == Path("/tmp/test_annotated.jpg")


def test_perception_defaults_screen_dims_to_zero() -> None:
    p = Perception(
        screenshot_path=Path("/tmp/test.jpg"),
        description="test",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
    )
    assert p.screen_width == 0
    assert p.screen_height == 0
    assert p.annotated_screenshot_path is None


class _SpyDetector:
    """Records detect() calls and returns a fixed annotation list (YOLO-style)."""

    def __init__(self, annotations: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.annotations = annotations or []
        self.seen_sizes: list[tuple[int, int]] = []

    def detect(self, image: Image.Image) -> list[dict[str, Any]]:
        self.calls += 1
        self.seen_sizes.append(image.size)
        return list(self.annotations)


def _patch_capture(module: PerceptionModule, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the blocking IO work so perceive() can run in tests."""
    # Instance attributes do not bind like methods, so lambdas take no `self`.
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (100, 100))
    )
    monkeypatch.setattr(
        module, "_run_ocr_detailed", lambda img: ("", [], img.size)
    )
    monkeypatch.setattr(
        module, "_generate_annotated", lambda path, ann: Image.new("RGB", (10, 10))
    )


@pytest.mark.asyncio
async def test_perceive_without_compensation_skips_detector(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No OCR text -> no UIA-less compensation -> detector never runs."""
    spy = _SpyDetector()
    module = PerceptionModule(config, detector=spy)
    _patch_capture(module, monkeypatch)

    result = await module.perceive(instruction="list files")

    assert spy.calls == 0
    assert result.som_annotations == []
    assert result.annotated_screenshot_path is None


@pytest.mark.asyncio
async def test_perceive_runs_ocr_on_full_resolution_image(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCR must see the pre-compression screenshot, not the 1280x720 copy
    destined for the LLM.

    Downscaling erases small text; OCR is local CPU work and costs no tokens,
    so it must run before _compress() thumbnails the image in place.
    """
    module = PerceptionModule(config)
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (1920, 1080))
    )
    seen_sizes: list[tuple[int, int]] = []

    def _record_ocr(img: Image.Image) -> tuple:
        seen_sizes.append(img.size)
        return "", [], img.size

    monkeypatch.setattr(module, "_run_ocr_detailed", _record_ocr)

    await module.perceive(instruction="x")

    assert seen_sizes == [(1920, 1080)]


def _record_ocr_input_size(module: PerceptionModule) -> list[tuple[int, int]]:
    """Install a fake OCR engine that records the temp image's dimensions."""
    seen: list[tuple[int, int]] = []

    def fake_ocr(path: str) -> tuple:
        with Image.open(path) as img:
            seen.append(img.size)
        # Real RapidOCR returns (items, elapse), not a bare list.
        return ([], [0.0, 0.0, 0.0])

    module._ocr = fake_ocr
    return seen


def test_run_ocr_uses_original_at_100_percent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 100% Windows scaling the original screenshot goes to OCR untouched:
    downscaling to 1080p only erases text that is already at native size."""
    module = PerceptionModule(config)
    seen = _record_ocr_input_size(module)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.0)

    module._run_ocr(Image.new("RGB", (2560, 1440)))

    assert seen == [(2560, 1440)]


def test_run_ocr_inverse_scales_at_125_percent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 125% scaling, OCR input is normalized by the inverse factor (0.8x):
    physically enlarged text is brought back to its 100% size."""
    module = PerceptionModule(config)
    seen = _record_ocr_input_size(module)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)

    module._run_ocr(Image.new("RGB", (2560, 1440)))

    assert seen == [(2048, 1152)]


def test_run_ocr_never_shrinks_below_1080p_cap(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse-scale result is floored at the old 1080p cap: extreme
    scaling (or a small virtualized capture) never gets a smaller image
    than plain 1080p capping would have produced."""
    module = PerceptionModule(config)
    seen = _record_ocr_input_size(module)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 3.0)

    module._run_ocr(Image.new("RGB", (3840, 2160)))

    assert seen == [(1920, 1080)]


def test_run_ocr_keeps_native_resolution_within_1080p(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Screens at or below 1080p pass through at native resolution (never
    upscaled), at any scaling factor."""
    module = PerceptionModule(config)
    seen = _record_ocr_input_size(module)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)

    module._run_ocr(Image.new("RGB", (1920, 1080)))
    module._run_ocr(Image.new("RGB", (1366, 768)))

    assert seen == [(1920, 1080), (1366, 768)]


def test_ocr_resize_ratio_formula() -> None:
    from agent.perception import _ocr_resize_ratio

    # 100%: original image, no resize.
    assert _ocr_resize_ratio((2560, 1440), 1.0) == 1.0
    # 125%: inverse factor 0.8.
    assert _ocr_resize_ratio((2560, 1440), 1.25) == pytest.approx(0.8)
    # 150% on 4K: 0.667 wins over the 0.5 cap.
    assert _ocr_resize_ratio((3840, 2160), 1.5) == pytest.approx(2 / 3, rel=1e-3)
    # 300% on 4K: floored at the 1080p cap (0.5).
    assert _ocr_resize_ratio((3840, 2160), 3.0) == pytest.approx(0.5)
    # Small images are never upscaled, at any scale.
    assert _ocr_resize_ratio((1280, 720), 1.25) == 1.0
    assert _ocr_resize_ratio((1280, 720), 2.0) == 1.0
    # Scale below 100% (shouldn't happen) is treated as 100%.
    assert _ocr_resize_ratio((2560, 1440), 0.9) == 1.0


def test_display_scale_falls_back_to_1_on_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import ctypes

    from agent.perception import _display_scale

    def _boom(*args, **kwargs):
        raise OSError("no display")

    monkeypatch.setattr(ctypes.windll.user32, "MonitorFromPoint", _boom)

    assert _display_scale() == 1.0


@pytest.mark.asyncio
async def test_perceive_compensation_detects_on_compressed_image(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """YOLO annotates the COMPRESSED (model-visible) image, so normalized
    annotation coordinates line up with the screenshot the model sees."""
    annotations = [
        {
            "label": 1,
            "center_x": 0.5,
            "center_y": 0.5,
            "bbox": [0.4, 0.4, 0.6, 0.6],
            "score": 0.9,
        },
    ]
    spy = _SpyDetector(annotations)
    module = PerceptionModule(config, detector=spy)
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (2560, 1440))
    )
    monkeypatch.setattr(
        module, "_run_ocr_detailed", lambda img: ("登录按钮", [], img.size)
    )
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)
    module.mcp = None  # empty UI tree -> compensation fires; real _compress runs

    result = await module.perceive(instruction="click OK")

    assert spy.calls == 1
    assert spy.seen_sizes == [(1920, 1080)]  # 2560x1440 at 125% -> 2048x1152 -> tiered to 1080p
    assert result.som_annotations == [
        {
            "label": 1,
            "center_x": 0.5,
            "center_y": 0.5,
            "bbox": [0.4, 0.4, 0.6, 0.6],
            "score": 0.9,
            "text": None,
            "icon": True,
        }
    ]


@pytest.mark.asyncio
async def test_perceive_survives_detector_failure(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A YOLO failure must not break perception: annotations stay empty."""

    class _Boom:
        def detect(self, image):
            raise RuntimeError("cuda boom")

    module = PerceptionModule(config, detector=_Boom())
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (100, 100))
    )
    monkeypatch.setattr(module, "_run_ocr_detailed", lambda img: ("text", [], img.size))
    module.mcp = None

    result = await module.perceive(instruction="x")

    assert result.som_annotations == []
    assert result.annotated_screenshot_path is None


# ---------------------------------------------------------------------------
# perceive_region (ZoomRegion backend)
# ---------------------------------------------------------------------------


def _patch_region_capture(
    module: PerceptionModule,
    monkeypatch: pytest.MonkeyPatch,
    size: tuple[int, int] = (2560, 1440),
    ocr_text: str = "区域文本",
) -> None:
    monkeypatch.setattr(
        module, "_capture_fullscreen", lambda: Image.new("RGB", size)
    )
    monkeypatch.setattr(
        module, "_run_ocr_detailed", lambda img: (ocr_text, [], img.size)
    )


@pytest.mark.asyncio
async def test_perceive_region_crops_and_sets_origin(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyDetector(
        [{"label": 1, "center_x": 0.5, "center_y": 0.5, "bbox": [0.4, 0.4, 0.6, 0.6], "score": 0.9}]
    )
    module = PerceptionModule(config, detector=spy)
    _patch_region_capture(module, monkeypatch)
    seen_ocr: list[tuple[int, int]] = []

    def _record_ocr(img: Image.Image) -> tuple:
        seen_ocr.append(img.size)
        return "text", [], img.size

    monkeypatch.setattr(module, "_run_ocr_detailed", _record_ocr)

    p = await module.perceive_region(1280, 720, 480)

    assert (p.screen_width, p.screen_height) == (480, 480)
    assert (p.screenshot_width, p.screenshot_height) == (480, 480)
    assert (p.image_origin_x, p.image_origin_y) == (1040, 480)
    assert seen_ocr == [(480, 480)]  # OCR ran on the crop
    assert spy.calls == 1
    assert spy.seen_sizes == [(480, 480)]  # YOLO ran on the crop
    assert len(p.som_annotations) == 1
    assert p.screenshot_path.exists()
    assert p.annotated_screenshot_path is not None
    assert p.annotated_screenshot_path.exists()
    assert "1040" in p.description and "480" in p.description


@pytest.mark.asyncio
async def test_perceive_region_clamps_box_at_screen_edges(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = PerceptionModule(config)
    _patch_region_capture(module, monkeypatch)

    top_left = await module.perceive_region(10, 10, 480)
    assert (top_left.image_origin_x, top_left.image_origin_y) == (0, 0)

    bottom_right = await module.perceive_region(2559, 1439, 480)
    assert (bottom_right.image_origin_x, bottom_right.image_origin_y) == (
        2080,
        960,
    )


@pytest.mark.asyncio
async def test_perceive_region_without_detector_still_works(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = PerceptionModule(config, detector=None)
    _patch_region_capture(module, monkeypatch)

    p = await module.perceive_region(1280, 720, 480)

    assert p.som_annotations == []
    assert p.annotated_screenshot_path is None
    assert p.screenshot_path.exists()
    assert p.ocr_text == "区域文本"


@pytest.mark.asyncio
async def test_perceive_region_small_screen_clamps_to_full(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Region larger than the screen degrades to the whole screen."""
    module = PerceptionModule(config)
    _patch_region_capture(module, monkeypatch, size=(400, 300))

    p = await module.perceive_region(200, 150, 480)

    assert (p.image_origin_x, p.image_origin_y) == (0, 0)
    assert (p.screen_width, p.screen_height) == (400, 300)


def test_compress_matches_ocr_at_100_percent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 100% scaling the inverse-DPI step passes through; the tiered
    downgrade then drops 3000x1500 (>2K) to the next lower tier (2K)."""
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.0)
    big = Image.new("RGB", (3000, 1500), (10, 20, 30))

    out = module._compress(big)

    assert Image.open(io.BytesIO(out)).size == (2560, 1280)


def test_compress_matches_ocr_at_125_percent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 125% scaling the screenshot is normalized by the inverse factor
    (0.8x → 2400x1200), then tiered down from >1080p to 1080p."""
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)
    big = Image.new("RGB", (3000, 1500), (10, 20, 30))

    out = module._compress(big)

    assert Image.open(io.BytesIO(out)).size == (1920, 960)


def test_compress_floored_at_1080p(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The inverse-scale result is floored at the 1080p box, same as OCR."""
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 3.0)
    big = Image.new("RGB", (3840, 2160), (10, 20, 30))

    out = module._compress(big)

    assert Image.open(io.BytesIO(out)).size == (1920, 1080)


def test_compress_original_resolution_skips_resize(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """UpgradeVision sets original_resolution: the screenshot is the
    original image (原画) regardless of display scaling."""
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 3.0)
    big = Image.new("RGB", (3840, 2160), (10, 20, 30))

    module.original_resolution = True
    out = module._compress(big)

    assert Image.open(io.BytesIO(out)).size == (3840, 2160)


class _SpyRapidOCR:
    """Stand-in for rapidocr_onnxruntime.RapidOCR recording constructor kwargs."""

    instances: list["_SpyRapidOCR"] = []

    def __init__(self, **kwargs):
        self.kwargs = kwargs
        _SpyRapidOCR.instances.append(self)

    def __call__(self, path: str) -> tuple:
        # Real RapidOCR returns (items, elapse), not a bare list.
        return ([], [0.0, 0.0, 0.0])


def test_run_ocr_passes_dml_flags_when_enabled(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ocr.use_dml on (default), all three OCR models get use_dml=True."""
    _SpyRapidOCR.instances.clear()
    monkeypatch.setattr("rapidocr_onnxruntime.RapidOCR", _SpyRapidOCR)
    config.ocr.use_dml = True
    module = PerceptionModule(config)

    module._run_ocr(Image.new("RGB", (100, 100)))

    assert _SpyRapidOCR.instances[0].kwargs == {
        "det_use_dml": True,
        "cls_use_dml": True,
        "rec_use_dml": True,
    }


def test_run_ocr_no_gpu_flags_when_dml_disabled(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """With ocr.use_dml off, RapidOCR is constructed with defaults (CPU)."""
    _SpyRapidOCR.instances.clear()
    monkeypatch.setattr("rapidocr_onnxruntime.RapidOCR", _SpyRapidOCR)
    config.ocr.use_dml = False
    module = PerceptionModule(config)

    module._run_ocr(Image.new("RGB", (100, 100)))

    assert _SpyRapidOCR.instances[0].kwargs == {}


def test_run_ocr_silences_ort_session_info_logs(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """rapidocr's OrtInferSession logger installs its own DEBUG StreamHandler
    and prints one INFO line per model on construction; _run_ocr must raise
    it to WARNING (keeping the DML-unavailable fallback warning visible)."""
    import logging

    _SpyRapidOCR.instances.clear()
    monkeypatch.setattr("rapidocr_onnxruntime.RapidOCR", _SpyRapidOCR)
    # Simulate rapidocr's own lru-cached configuration having run.
    monkeypatch.setattr(
        logging.getLogger("OrtInferSession"), "level", logging.DEBUG
    )
    module = PerceptionModule(config)

    module._run_ocr(Image.new("RGB", (100, 100)))

    assert logging.getLogger("OrtInferSession").level == logging.WARNING


# ---------------------------------------------------------------------------
# Description marker listing: the model must see each marker's number AND its
# content (OCR text / icon type) so it can pick a label without guessing.
# ---------------------------------------------------------------------------


def _ann(label, cx, cy, text=None, icon=True, score=0.9):
    return {
        "label": label,
        "center_x": cx,
        "center_y": cy,
        "bbox": [cx - 0.05, cy - 0.05, cx + 0.05, cy + 0.05],
        "score": score,
        "text": text,
        "icon": icon,
    }


def test_build_description_lists_marker_content() -> None:
    anns = [
        _ann(1, 0.73, 0.405, text="搜索", icon=True),   # merged OCR+YOLO
        _ann(2, 0.12, 0.88, text="视频号", icon=False),  # OCR-only
        _ann(3, 0.5, 0.1, text=None, icon=True),         # YOLO-only
    ]

    desc = PerceptionModule._build_description("", {}, anns, (1000, 1000))

    assert '[1] "搜索" icon @(0.7300,0.4050)' in desc
    assert '[2] "视频号" text @(0.1200,0.8800)' in desc
    assert "[3] icon @(0.5000,0.1000)" in desc


def test_build_description_caps_marker_list_at_100() -> None:
    anns = [_ann(i, 0.1, 0.1) for i in range(1, 106)]

    desc = PerceptionModule._build_description("", {}, anns, (100, 100))

    assert "[100] icon" in desc
    assert "[101]" not in desc
    assert "(+5 more markers)" in desc


def test_build_region_description_lists_marker_content() -> None:
    anns = [_ann(1, 0.3, 0.4, text="确定", icon=True)]

    desc = PerceptionModule._build_region_description(
        "确定", anns, (1040, 480), (480, 480)
    )

    assert '[1] "确定" icon @(0.3000,0.4000)' in desc


# ---------------------------------------------------------------------------
# perceive() fusion integration
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_perceive_marks_ocr_boxes_without_yolo(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """OCR boxes become SoM markers even when no YOLO detector exists — and
    the annotated screenshot is still generated (it needs no detector)."""
    module = PerceptionModule(config, detector=None)
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (1000, 1000))
    )
    monkeypatch.setattr(
        module,
        "_run_ocr_detailed",
        lambda img: (
            "搜索",
            [{"bbox": [100.0, 100.0, 200.0, 150.0], "text": "搜索", "score": 0.99}],
            img.size,
        ),
    )
    monkeypatch.setattr(
        module, "_generate_annotated", lambda path, ann: Image.new("RGB", (10, 10))
    )

    p = await module.perceive(instruction="x")

    assert len(p.som_annotations) == 1
    a = p.som_annotations[0]
    assert a["text"] == "搜索"
    assert a["icon"] is False
    assert a["bbox"] == pytest.approx([0.1, 0.1, 0.2, 0.15])
    assert p.annotated_screenshot_path is not None
    assert p.annotated_screenshot_path.exists()
    assert '[1] "搜索" text @(0.1500,0.1250)' in p.description


@pytest.mark.asyncio
async def test_perceive_merges_ocr_text_into_yolo_marker(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """On a UIA-less screen, an OCR box overlapping a YOLO box (IoU > 15%)
    fuses into ONE marker carrying both the text and the icon flag."""
    spy = _SpyDetector(
        [{"label": 1, "center_x": 0.15, "center_y": 0.125,
          "bbox": [0.1, 0.1, 0.2, 0.15], "score": 0.9}]
    )
    module = PerceptionModule(config, detector=spy)
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (1000, 1000))
    )
    monkeypatch.setattr(
        module,
        "_run_ocr_detailed",
        lambda img: (
            "登录",
            [{"bbox": [110.0, 110.0, 190.0, 140.0], "text": "登录", "score": 0.99}],
            img.size,
        ),
    )
    module.mcp = None  # empty UI tree + OCR text -> YOLO compensation fires

    p = await module.perceive(instruction="click 登录")

    assert spy.calls == 1
    assert len(p.som_annotations) == 1
    a = p.som_annotations[0]
    assert a["text"] == "登录"
    assert a["icon"] is True
    assert a["score"] == pytest.approx(0.99)
    assert '[1] "登录" icon' in p.description


@pytest.mark.asyncio
async def test_perceive_region_fuses_ocr_and_yolo(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyDetector(
        [{"label": 1, "center_x": 0.15, "center_y": 0.15,
          "bbox": [0.1, 0.1, 0.2, 0.2], "score": 0.9}]
    )
    module = PerceptionModule(config, detector=spy)
    monkeypatch.setattr(
        module, "_capture_fullscreen", lambda: Image.new("RGB", (2560, 1440))
    )
    monkeypatch.setattr(
        module,
        "_run_ocr_detailed",
        lambda img: (
            "OK",
            [{"bbox": [48.0, 48.0, 96.0, 96.0], "text": "OK", "score": 0.95}],
            img.size,
        ),
    )

    p = await module.perceive_region(1280, 720, 480)

    assert len(p.som_annotations) == 1
    a = p.som_annotations[0]
    assert a["text"] == "OK"
    assert a["icon"] is True
    assert a["bbox"] == pytest.approx([0.1, 0.1, 0.2, 0.2])
    assert '[1] "OK" icon' in p.description


# ---------------------------------------------------------------------------
# Florence-2 icon captioning: bare icon markers get a semantic caption as
# their text (merged markers keep the more precise OCR text).
# ---------------------------------------------------------------------------


class _SpyCaptioner:
    """Stand-in for IconCaptioner: records calls, writes canned captions."""

    def __init__(self, boom: bool = False) -> None:
        self.calls: list[dict] = []
        self.boom = boom

    def caption_markers(self, image, markers, max_icons):
        self.calls.append(
            {"size": image.size, "n": len(markers), "max_icons": max_icons}
        )
        if self.boom:
            raise RuntimeError("florence boom")
        count = 0
        for m in markers:
            if m.get("icon") and not m.get("text"):
                m["text"] = f"caption-{m['label']}"
                count += 1
        return count


@pytest.mark.asyncio
async def test_perceive_captions_bare_icon_markers(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bare YOLO icon (no overlapping OCR) gets a Florence-2 caption as its
    text; a merged marker keeps the OCR text. The caption lands in the
    model-facing description."""
    spy = _SpyDetector(
        [{"label": 1, "center_x": 0.5, "center_y": 0.5,
          "bbox": [0.4, 0.4, 0.6, 0.6], "score": 0.9}]
    )
    captioner = _SpyCaptioner()
    module = PerceptionModule(config, detector=spy, captioner=captioner)
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (1000, 1000))
    )
    monkeypatch.setattr(
        module,
        "_run_ocr_detailed",
        lambda img: (
            "登录",
            [{"bbox": [10.0, 10.0, 100.0, 40.0], "text": "登录", "score": 0.99}],
            img.size,
        ),
    )
    module.mcp = None  # UIA-less -> YOLO compensation fires

    p = await module.perceive(instruction="x")

    assert len(captioner.calls) == 1
    assert captioner.calls[0]["size"] == (1000, 1000)  # model-visible image
    assert captioner.calls[0]["max_icons"] == config.icon_caption.max_icons
    by_label = {a["label"]: a for a in p.som_annotations}
    assert by_label[1]["text"] == "登录"          # OCR text marker: untouched
    assert by_label[2]["text"] == "caption-2"     # bare icon: captioned
    assert '[2] "caption-2" icon' in p.description


@pytest.mark.asyncio
async def test_perceive_caption_failure_degrades_gracefully(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A captioner failure must not break perception: markers keep text=None
    and the annotated screenshot is still produced."""
    spy = _SpyDetector(
        [{"label": 1, "center_x": 0.5, "center_y": 0.5,
          "bbox": [0.4, 0.4, 0.6, 0.6], "score": 0.9}]
    )
    module = PerceptionModule(
        config, detector=spy, captioner=_SpyCaptioner(boom=True)
    )
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (100, 100))
    )
    monkeypatch.setattr(
        module, "_run_ocr_detailed", lambda img: ("text", [], img.size)
    )
    module.mcp = None

    p = await module.perceive(instruction="x")

    assert len(p.som_annotations) == 1
    assert p.som_annotations[0]["text"] is None
    assert p.annotated_screenshot_path is not None


@pytest.mark.asyncio
async def test_perceive_region_captions_bare_icons(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyDetector(
        [{"label": 1, "center_x": 0.5, "center_y": 0.5,
          "bbox": [0.4, 0.4, 0.6, 0.6], "score": 0.9}]
    )
    captioner = _SpyCaptioner()
    module = PerceptionModule(config, detector=spy, captioner=captioner)
    monkeypatch.setattr(
        module, "_capture_fullscreen", lambda: Image.new("RGB", (2560, 1440))
    )
    monkeypatch.setattr(
        module, "_run_ocr_detailed", lambda img: ("", [], img.size)
    )

    p = await module.perceive_region(1280, 720, 480)

    assert len(captioner.calls) == 1
    assert captioner.calls[0]["size"] == (480, 480)  # the region crop
    assert p.som_annotations[0]["text"] == "caption-1"
