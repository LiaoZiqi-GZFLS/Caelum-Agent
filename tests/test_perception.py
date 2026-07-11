"""Tests for perception fusion and UI hashing."""

from __future__ import annotations

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
    """OCR must see the original screenshot, not the 800x600 compressed copy.

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
