"""Tests for the CaptureWindow tool (agent/window_capture.py)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

from agent.window_capture import (
    WindowCapturer,
    make_capture_window_handler,
    register_capture_window,
)


def _png_bytes(size: tuple[int, int] = (60, 40)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (50, 100, 150)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeUploader:
    def __init__(self) -> None:
        self.uploads: list[Path] = []

    async def upload(self, path: str | Path) -> tuple[str, str]:
        self.uploads.append(Path(path))
        return "image", f"ms://win-{len(self.uploads)}"


def _capturer(
    tmp_path: Path,
    windows: list[tuple[int, str]] | None = None,
    fail_capture: bool = False,
) -> WindowCapturer:
    """WindowCapturer with injected fakes (no real win32 calls)."""
    listing = windows or [(101, "微信"), (102, "记事本")]

    def fake_list() -> list[tuple[int, str]]:
        return listing

    def fake_capture(hwnd: int, out: Path) -> tuple[int, int, int, int]:
        if fail_capture:
            raise RuntimeError("PrintWindow returned empty bitmap")
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_png_bytes())
        return (10, 20, 300, 200)  # window rect in screen space

    return WindowCapturer(
        tmp_path / "captures",
        list_windows=fake_list,
        capture=fake_capture,
    )


@pytest.mark.asyncio
async def test_handler_captures_and_returns_media_ref(tmp_path: Path) -> None:
    uploader = _FakeUploader()
    handler = make_capture_window_handler(_capturer(tmp_path), uploader)

    result = await handler(title="微信")

    assert result.startswith("[media_ref] image ms://win-1")
    assert len(uploader.uploads) == 1
    assert uploader.uploads[0].exists()


@pytest.mark.asyncio
async def test_handler_prefers_exact_title_match(tmp_path: Path) -> None:
    uploader = _FakeUploader()
    capturer = _capturer(tmp_path, windows=[(101, "微信"), (102, "微信(传输文件)")])

    chosen: list[int] = []
    orig_capture = capturer._capture

    def spy(hwnd: int, out: Path) -> None:
        chosen.append(hwnd)
        orig_capture(hwnd, out)

    capturer._capture = spy
    handler = make_capture_window_handler(capturer, uploader)
    await handler(title="微信")

    assert chosen == [101]  # exact match wins over substring match


@pytest.mark.asyncio
async def test_handler_lists_titles_when_not_found(tmp_path: Path) -> None:
    handler = make_capture_window_handler(_capturer(tmp_path), _FakeUploader())

    result = await handler(title="飞书")

    assert result.startswith("[error]")
    assert "微信" in result and "记事本" in result  # available titles hint


@pytest.mark.asyncio
async def test_handler_reports_capture_failure(tmp_path: Path) -> None:
    handler = make_capture_window_handler(
        _capturer(tmp_path, fail_capture=True), _FakeUploader()
    )

    result = await handler(title="微信")

    assert result.startswith("[error]")
    assert "PrintWindow" in result


def test_capture_by_title_returns_window_rect(tmp_path: Path) -> None:
    full, path, rect = _capturer(tmp_path).capture_by_title("微信")

    assert full == "微信"
    assert path.exists()
    assert rect == (10, 20, 300, 200)  # screen rect from the capture


@pytest.mark.asyncio
async def test_handler_reports_view_to_on_capture(tmp_path: Path) -> None:
    views: list = []
    handler = make_capture_window_handler(
        _capturer(tmp_path), _FakeUploader(),
        on_capture=lambda rect, size: views.append((rect, size)),
    )

    result = await handler(title="微信")

    assert len(views) == 1
    rect, image_size = views[0]
    assert rect == (10, 20, 300, 200)
    # The model sees the IMAGE, so the image's real pixel size is the loc space.
    assert image_size == (60, 40)
    assert "Conversion to screen coordinates" in result


@pytest.mark.asyncio
async def test_handler_without_rect_skips_on_capture(tmp_path: Path) -> None:
    # A legacy capture returning None provides no view; on_capture stays silent.
    capturer = _capturer(tmp_path)

    def legacy_capture(hwnd: int, out: Path):
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(_png_bytes())
        return None

    capturer._capture = legacy_capture
    views: list = []
    handler = make_capture_window_handler(
        capturer, _FakeUploader(),
        on_capture=lambda rect, size: views.append((rect, size)),
    )

    result = await handler(title="微信")

    assert result.startswith("[media_ref]")
    assert views == []


class _RecordingLLM:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_local_function(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.registered[name] = {"handler": handler, **kwargs}


def test_register_capture_window(tmp_path: Path) -> None:
    from agent.config import LLMConfig

    llm = _RecordingLLM()
    capturer = register_capture_window(
        llm, LLMConfig(api_key="sk-test"), tmp_path / "cache",
        uploader=_FakeUploader(),
    )

    assert capturer is not None
    assert "CaptureWindow" in llm.registered
    assert llm.registered["CaptureWindow"]["schema"]["required"] == ["title"]


def test_register_capture_window_requires_uploader(tmp_path: Path) -> None:
    from agent.config import LLMConfig

    llm = _RecordingLLM()
    capturer = register_capture_window(
        llm, LLMConfig(api_key="sk-test"), tmp_path / "cache", uploader=None
    )

    assert capturer is None
    assert llm.registered == {}
