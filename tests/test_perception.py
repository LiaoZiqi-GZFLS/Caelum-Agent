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
            "model": "kimi-k2.6",
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
    """Records annotate() calls and returns a fixed annotation list."""

    def __init__(self, annotations: list[dict[str, Any]] | None = None) -> None:
        self.calls = 0
        self.annotations = annotations or []

    async def annotate(
        self, image: Image.Image, instruction: str
    ) -> tuple[list[dict[str, Any]], int]:
        self.calls += 1
        return list(self.annotations), 0


def _patch_capture(module: PerceptionModule, monkeypatch: pytest.MonkeyPatch) -> None:
    """Stub out the blocking IO work so perceive() can run in tests."""
    # Instance attributes do not bind like methods, so lambdas take no `self`.
    monkeypatch.setattr(
        module, "_capture_screenshot", lambda: Image.new("RGB", (100, 100))
    )
    monkeypatch.setattr(module, "_run_ocr", lambda img: "")
    monkeypatch.setattr(
        module, "_generate_annotated", lambda path, ann: Image.new("RGB", (10, 10))
    )


@pytest.mark.asyncio
async def test_perceive_without_vision_skips_detector(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyDetector()
    module = PerceptionModule(config, ui_detector=spy)
    _patch_capture(module, monkeypatch)

    result = await module.perceive(instruction="list files", with_vision=False)

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
    monkeypatch.setattr(
        module, "_run_ocr", lambda img: seen_sizes.append(img.size) or ""
    )

    await module.perceive(instruction="x", with_vision=False)

    assert seen_sizes == [(1920, 1080)]


def _record_ocr_input_size(module: PerceptionModule) -> list[tuple[int, int]]:
    """Install a fake OCR engine that records the temp image's dimensions."""
    seen: list[tuple[int, int]] = []

    def fake_ocr(path: str) -> list:
        with Image.open(path) as img:
            seen.append(img.size)
        return []

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
async def test_perceive_with_vision_runs_detector(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.9, "normalized": True},
    ]
    spy = _SpyDetector(annotations)
    module = PerceptionModule(config, ui_detector=spy)
    _patch_capture(module, monkeypatch)

    result = await module.perceive(instruction="click OK", with_vision=True)

    assert spy.calls == 1
    assert result.som_annotations == annotations


@pytest.mark.asyncio
async def test_perceive_with_vision_helper(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    spy = _SpyDetector()
    module = PerceptionModule(config, ui_detector=spy)
    _patch_capture(module, monkeypatch)

    await module.perceive_with_vision("click OK")

    assert spy.calls == 1


def test_compress_matches_ocr_at_100_percent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The model-facing screenshot uses the SAME inverse-DPI rule as OCR:
    at 100% scaling the original image passes through untouched."""
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.0)
    big = Image.new("RGB", (3000, 1500), (10, 20, 30))

    out = module._compress(big)

    assert Image.open(io.BytesIO(out)).size == (3000, 1500)


def test_compress_matches_ocr_at_125_percent(
    config: Config, monkeypatch: pytest.MonkeyPatch
) -> None:
    """At 125% scaling the screenshot is normalized by the inverse factor
    (0.8x), exactly like OCR input."""
    module = PerceptionModule(config)
    monkeypatch.setattr("agent.perception._display_scale", lambda: 1.25)
    big = Image.new("RGB", (3000, 1500), (10, 20, 30))

    out = module._compress(big)

    assert Image.open(io.BytesIO(out)).size == (2400, 1200)


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

    def __call__(self, path: str) -> list:
        return []


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
