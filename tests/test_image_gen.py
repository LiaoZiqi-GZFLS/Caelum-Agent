"""Tests for the GenerateImage subagent tool (agent/image_gen.py)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.image_gen import (
    ImageGenerator,
    extract_svg,
    make_generate_image_handler,
    register_generate_image,
)

SVG_A = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><rect width="100" height="100" fill="red"/></svg>'
SVG_B = '<svg xmlns="http://www.w3.org/2000/svg" width="100" height="100"><circle cx="50" cy="50" r="40" fill="green"/></svg>'


def _msg(content: str) -> Any:
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content, tool_calls=[]))]
    )


def _verdict(ok: bool, issues: str = "") -> Any:
    return _msg(json.dumps({"ok": ok, "issues": issues}))


class _FakeLLM:
    def __init__(self, responses: list[Any]) -> None:
        self._queue = list(responses)
        self.calls: list[list[dict[str, Any]]] = []

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> Any:
        self.calls.append(messages)
        if not self._queue:
            raise RuntimeError("FakeLLM ran out of responses")
        return self._queue.pop(0)


class _FakeUploader:
    def __init__(self) -> None:
        self.uploads: list[Path] = []

    async def upload(self, path: str | Path) -> tuple[str, str]:
        self.uploads.append(Path(path))
        return "image", f"ms://fake-{len(self.uploads)}"


def _gen(llm: _FakeLLM, uploader: _FakeUploader, tmp_path: Path, **kw: Any) -> ImageGenerator:
    return ImageGenerator(llm, uploader, tmp_path / "generated", **kw)


# ---------------------------------------------------------------------------
# extract_svg
# ---------------------------------------------------------------------------

def test_extract_svg_strips_code_fences() -> None:
    text = f"Here you go:\n```svg\n{SVG_A}\n```\nDone."
    assert extract_svg(text) == SVG_A


def test_extract_svg_bare_markup() -> None:
    assert extract_svg(SVG_B) == SVG_B


def test_extract_svg_returns_none_without_svg() -> None:
    assert extract_svg("I cannot draw that.") is None


# ---------------------------------------------------------------------------
# generation loop
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_generate_happy_path(tmp_path: Path) -> None:
    llm = _FakeLLM([_msg(f"```svg\n{SVG_A}\n```"), _verdict(True)])
    uploader = _FakeUploader()

    result = await _gen(llm, uploader, tmp_path).generate("a red square")

    assert result["ok"] is True
    assert result["rounds"] == 1
    assert Path(result["path"]).exists()
    assert len(uploader.uploads) == 1


@pytest.mark.asyncio
async def test_generate_retries_with_feedback(tmp_path: Path) -> None:
    llm = _FakeLLM([
        _msg(SVG_A), _verdict(False, "wanted a circle, not a square"),
        _msg(SVG_B), _verdict(True),
    ])
    uploader = _FakeUploader()

    result = await _gen(llm, uploader, tmp_path).generate("a green circle")

    assert result["ok"] is True
    assert result["rounds"] == 2
    # The retry message must carry the reviewer's feedback.
    retry_user_msg = llm.calls[2][-1]["content"]
    assert "wanted a circle" in retry_user_msg


@pytest.mark.asyncio
async def test_generate_gives_up_after_max_rounds(tmp_path: Path) -> None:
    llm = _FakeLLM([m for _ in range(5) for m in (_msg(SVG_A), _verdict(False, "still wrong"))])
    uploader = _FakeUploader()

    result = await _gen(llm, uploader, tmp_path).generate("something hard")

    assert result["ok"] is False
    assert result["rounds"] == 5
    assert result["issues"] == "still wrong"
    assert Path(result["path"]).exists()  # last attempt still returned
    assert len(uploader.uploads) == 5


@pytest.mark.asyncio
async def test_generate_handles_render_failure_as_feedback(tmp_path: Path) -> None:
    llm = _FakeLLM([
        _msg("<svg><broken"),            # not valid SVG -> render fails
        _msg(SVG_B), _verdict(True),
    ])
    uploader = _FakeUploader()

    result = await _gen(llm, uploader, tmp_path).generate("a green circle")

    assert result["ok"] is True
    assert result["rounds"] == 2
    assert len(uploader.uploads) == 1  # broken SVG never reached upload


@pytest.mark.asyncio
async def test_generate_handles_missing_svg_as_feedback(tmp_path: Path) -> None:
    llm = _FakeLLM([
        _msg("Sorry, I drew nothing."),
        _msg(SVG_A), _verdict(True),
    ])
    uploader = _FakeUploader()

    result = await _gen(llm, uploader, tmp_path).generate("a red square")

    assert result["ok"] is True
    assert result["rounds"] == 2


@pytest.mark.asyncio
async def test_generate_tolerates_unparseable_verdict(tmp_path: Path) -> None:
    llm = _FakeLLM([
        _msg(SVG_A), _msg("Looks bad, the colors are off"),  # not JSON
        _msg(SVG_B), _verdict(True),
    ])
    uploader = _FakeUploader()

    result = await _gen(llm, uploader, tmp_path).generate("a circle")

    assert result["ok"] is True
    assert result["rounds"] == 2


# ---------------------------------------------------------------------------
# tool handler / registration
# ---------------------------------------------------------------------------

@pytest.mark.asyncio
async def test_handler_returns_path_and_status(tmp_path: Path) -> None:
    llm = _FakeLLM([_msg(SVG_A), _verdict(True)])
    handler = make_generate_image_handler(_gen(llm, _FakeUploader(), tmp_path))

    result = await handler(requirement="a red square")

    assert "generated" in result.lower() or "round" in result.lower()
    assert str(tmp_path) in result


class _RecordingLLM:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_local_function(self, name: str, handler: Any, **kwargs: Any) -> None:
        self.registered[name] = {"handler": handler, **kwargs}


def test_register_generate_image(tmp_path: Path) -> None:
    from agent.config import LLMConfig

    llm = _RecordingLLM()
    generator = register_generate_image(
        llm, LLMConfig(api_key="sk-test"), tmp_path / "cache", uploader=_FakeUploader()
    )

    assert generator is not None
    assert "GenerateImage" in llm.registered
    assert llm.registered["GenerateImage"]["schema"]["required"] == ["requirement"]


def test_register_generate_image_requires_uploader(tmp_path: Path) -> None:
    from agent.config import LLMConfig

    llm = _RecordingLLM()
    generator = register_generate_image(
        llm, LLMConfig(api_key="sk-test"), tmp_path / "cache", uploader=None
    )

    assert generator is None
    assert llm.registered == {}
