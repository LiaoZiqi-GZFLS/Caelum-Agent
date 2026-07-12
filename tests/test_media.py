"""Tests for image/video upload via the Kimi Files API (agent/media.py)."""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import pytest
from PIL import Image

import agent.media as media_mod
from agent.media import (
    IMAGE_EXTENSIONS,
    VIDEO_EXTENSIONS,
    MediaUploader,
    make_view_media_handler,
    parse_media_refs,
    register_view_media,
)


def _png_bytes(size: tuple[int, int] = (100, 80)) -> bytes:
    buf = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buf, format="PNG")
    return buf.getvalue()


class _FakeResponse:
    def __init__(self, json_data: dict[str, Any] | None = None) -> None:
        self._json = json_data or {}

    def json(self) -> dict[str, Any]:
        return self._json

    def raise_for_status(self) -> None:
        pass


class _FakeHTTP:
    def __init__(self, file_id: str = "file-xyz") -> None:
        self.file_id = file_id
        self.posts: list[dict[str, Any]] = []
        self.deletes: list[str] = []
        self.list_payload: dict[str, Any] = {"data": []}

    async def post(self, url: str, **kwargs: Any) -> _FakeResponse:
        name, fh = kwargs["files"]["file"]
        self.posts.append({
            "url": url,
            "purpose": kwargs["data"]["purpose"],
            "name": name,
            "bytes": fh.read(),
        })
        return _FakeResponse({"id": self.file_id})

    async def get(self, url: str, **kwargs: Any) -> _FakeResponse:
        return _FakeResponse(json_data=self.list_payload)

    async def delete(self, url: str, **kwargs: Any) -> _FakeResponse:
        self.deletes.append(url)
        return _FakeResponse({})


def _uploader(tmp_path: Path, http: _FakeHTTP | None = None, **kwargs: Any) -> MediaUploader:
    return MediaUploader(
        base_url="https://api.moonshot.cn/v1",
        api_key="sk-test",
        work_dir=tmp_path / "media",
        http=http or _FakeHTTP(),
        **kwargs,
    )


@pytest.mark.asyncio
async def test_upload_image_returns_ms_ref(tmp_path: Path) -> None:
    http = _FakeHTTP()
    img = tmp_path / "shot.png"
    raw = _png_bytes()
    img.write_bytes(raw)

    kind, url = await _uploader(tmp_path, http).upload(img)

    assert kind == "image"
    assert url == "ms://file-xyz"
    assert http.posts[0]["purpose"] == "image"
    assert http.posts[0]["bytes"] == raw  # small image uploaded unmodified


@pytest.mark.asyncio
async def test_upload_rejects_unsupported_extension(tmp_path: Path) -> None:
    f = tmp_path / "notes.txt"
    f.write_text("hello", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported"):
        await _uploader(tmp_path).upload(f)


@pytest.mark.asyncio
async def test_upload_rejects_missing_file(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        await _uploader(tmp_path).upload(tmp_path / "nope.png")


@pytest.mark.asyncio
async def test_upload_rejects_oversized_file(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(media_mod, "MAX_UPLOAD_BYTES", 10)
    img = tmp_path / "big.png"
    img.write_bytes(_png_bytes())  # a few hundred bytes > 10

    with pytest.raises(ValueError, match="100 MB"):
        await _uploader(tmp_path).upload(img)


@pytest.mark.asyncio
async def test_image_over_4k_downscaled(tmp_path: Path) -> None:
    http = _FakeHTTP()
    img = tmp_path / "huge.png"
    img.write_bytes(_png_bytes((5000, 3000)))

    kind, _ = await _uploader(tmp_path, http).upload(img)

    assert kind == "image"
    with Image.open(io.BytesIO(http.posts[0]["bytes"])) as im:
        assert im.width <= 3840
        assert im.height <= 2160
        assert im.width < 5000  # actually shrunk, aspect preserved


@pytest.mark.asyncio
async def test_video_compressed_before_upload(tmp_path: Path) -> None:
    calls: list[tuple[Path, Path]] = []

    async def fake_compressor(src: Path, dst: Path) -> Path:
        calls.append((src, dst))
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"compressed mp4")
        return dst

    http = _FakeHTTP()
    video = tmp_path / "clip.mov"
    video.write_bytes(b"raw huge video bytes")

    kind, url = await _uploader(tmp_path, http, video_compressor=fake_compressor).upload(video)

    assert kind == "video"
    assert url == "ms://file-xyz"
    assert len(calls) == 1
    assert http.posts[0]["purpose"] == "video"
    assert http.posts[0]["bytes"] == b"compressed mp4"


@pytest.mark.asyncio
async def test_video_compression_cached(tmp_path: Path) -> None:
    calls = 0

    async def fake_compressor(src: Path, dst: Path) -> Path:
        nonlocal calls
        calls += 1
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"compressed")
        return dst

    video = tmp_path / "clip.mp4"
    video.write_bytes(b"same video")
    uploader = _uploader(tmp_path, video_compressor=fake_compressor)

    await uploader.upload(video)
    await uploader.upload(video)

    assert calls == 1  # second upload reuses the cached compression


@pytest.mark.asyncio
async def test_video_oversized_after_compression_rejected(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def fake_compressor(src: Path, dst: Path) -> Path:
        dst.parent.mkdir(parents=True, exist_ok=True)
        dst.write_bytes(b"x" * 20)
        return dst

    monkeypatch.setattr(media_mod, "MAX_UPLOAD_BYTES", 10)
    video = tmp_path / "clip.mp4"
    video.write_bytes(b"video")

    with pytest.raises(ValueError, match="100 MB"):
        await _uploader(tmp_path, video_compressor=fake_compressor).upload(video)


@pytest.mark.asyncio
async def test_ffmpeg_compressor_requires_ffmpeg(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(media_mod.shutil, "which", lambda name: None)
    with pytest.raises(RuntimeError, match="ffmpeg"):
        await media_mod._ffmpeg_compress_video(
            tmp_path / "in.mp4", tmp_path / "out.mp4"
        )


def test_ffmpeg_command_targets_15fps_1080p(monkeypatch: pytest.MonkeyPatch) -> None:
    """The real compressor must request 15fps and a 1080p downscale."""
    captured: list[list[str]] = []

    class _FakeProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_exec(*args: str, **kwargs: Any) -> _FakeProc:
        captured.append(list(args))
        return _FakeProc()

    monkeypatch.setattr(media_mod.shutil, "which", lambda name: "/usr/bin/ffmpeg")
    monkeypatch.setattr(media_mod.asyncio, "create_subprocess_exec", fake_exec)

    import asyncio

    asyncio.run(media_mod._ffmpeg_compress_video(Path("in.mp4"), Path("out.mp4")))

    cmd = " ".join(captured[0])
    assert "fps=15" in cmd
    assert "1920" in cmd  # 1080p = 1920x1080 bounding box


@pytest.mark.asyncio
async def test_handler_returns_media_ref_marker(tmp_path: Path) -> None:
    img = tmp_path / "shot.png"
    img.write_bytes(_png_bytes())
    handler = make_view_media_handler(_uploader(tmp_path))

    result = await handler(path=str(img))

    assert result.startswith("[media_ref] image ms://file-xyz")


@pytest.mark.asyncio
async def test_handler_returns_error_string_on_failure(tmp_path: Path) -> None:
    handler = make_view_media_handler(_uploader(tmp_path))

    result = await handler(path=str(tmp_path / "missing.png"))

    assert result.startswith("[error]")


def test_parse_media_refs() -> None:
    text = "[media_ref] video ms://files/abc123\nattached"
    assert parse_media_refs(text) == [("video", "ms://files/abc123")]
    assert parse_media_refs("no marker here") == []


@pytest.mark.asyncio
async def test_sweep_deletes_image_and_video_only(tmp_path: Path) -> None:
    http = _FakeHTTP()
    http.list_payload = {
        "data": [
            {"id": "f1", "purpose": "image"},
            {"id": "f2", "purpose": "video"},
            {"id": "f3", "purpose": "file-extract"},
        ]
    }

    deleted = await _uploader(tmp_path, http).sweep_remote()

    assert deleted == 2
    assert http.deletes == [
        "https://api.moonshot.cn/v1/files/f1",
        "https://api.moonshot.cn/v1/files/f2",
    ]


def test_extension_sets_cover_documented_formats() -> None:
    assert {".png", ".jpeg", ".webp", ".gif"} <= IMAGE_EXTENSIONS
    assert {".mp4", ".mov", ".avi", ".webm", ".wmv"} <= VIDEO_EXTENSIONS
    assert IMAGE_EXTENSIONS.isdisjoint(VIDEO_EXTENSIONS)


class _RecordingLLM:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_local_function(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.registered[name] = {"handler": handler, **kwargs}


def test_register_view_media_registers_tool_when_enabled(tmp_path: Path) -> None:
    from agent.config import LLMConfig

    llm = _RecordingLLM()
    uploader = register_view_media(llm, LLMConfig(api_key="sk-test"), tmp_path / "cache")

    assert uploader is not None
    assert "ViewMedia" in llm.registered
    assert llm.registered["ViewMedia"]["schema"]["required"] == ["path"]


def test_register_view_media_skips_when_disabled(tmp_path: Path) -> None:
    from agent.config import LLMConfig

    llm = _RecordingLLM()
    uploader = register_view_media(
        llm, LLMConfig(api_key="sk-test", enable_media_upload=False), tmp_path / "cache"
    )

    assert uploader is None
    assert llm.registered == {}
