"""Tests for the DraftContent subagent tool (agent/content_writer.py)."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from agent.content_writer import (
    DRAFT_CONTENT_SCHEMA,
    make_draft_content_handler,
    register_draft_content,
)
from tests.fakes import FakeLLM


def _completion(text: str) -> Any:
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(content=text), finish_reason="stop"
            )
        ]
    )


@pytest.mark.asyncio
async def test_draft_writes_file_and_returns_preview(tmp_path: Path) -> None:
    llm = FakeLLM(chat_responses=[_completion("world of content")])
    handler = make_draft_content_handler(llm, tmp_path / "drafts")

    result = await handler(
        task="write an article", persona="tech writer", prefill="hello "
    )

    # File contains prefill + continuation.
    drafts = list((tmp_path / "drafts").glob("*.md"))
    assert len(drafts) == 1
    assert drafts[0].read_text(encoding="utf-8") == "hello world of content"
    # Result points at the file, reports size, previews, and hints at the
    # context-free clipboard path.
    assert str(drafts[0]) in result
    assert "22 chars" in result
    assert "Set-Clipboard" in result
    assert "hello world" in result


@pytest.mark.asyncio
async def test_draft_sends_partial_prefill_message(tmp_path: Path) -> None:
    llm = FakeLLM(chat_responses=[_completion("body")])
    handler = make_draft_content_handler(llm, tmp_path / "drafts")

    await handler(task="continue this", persona="writer", prefill="# Title\n\n")

    messages = llm.calls[0]
    assert messages[0]["role"] == "system"
    assert messages[0]["content"] == "writer"
    assert messages[1]["role"] == "user"
    assert messages[1]["content"] == "continue this"
    last = messages[-1]
    assert last["role"] == "assistant"
    assert last["partial"] is True
    assert last["content"] == "# Title\n\n"
    # Subagent must run tool-free.
    assert llm.last_tools[0] is None


@pytest.mark.asyncio
async def test_draft_without_prefill_omits_partial_message(tmp_path: Path) -> None:
    llm = FakeLLM(chat_responses=[_completion("fresh text")])
    handler = make_draft_content_handler(llm, tmp_path / "drafts")

    await handler(task="write from scratch", persona="writer")

    assert llm.calls[0][-1]["role"] == "user"


@pytest.mark.asyncio
async def test_draft_truncates_at_max_chars(tmp_path: Path) -> None:
    llm = FakeLLM(chat_responses=[_completion("x" * 100)])
    handler = make_draft_content_handler(llm, tmp_path / "drafts")

    result = await handler(task="t", persona="p", max_chars=30)

    draft = next((tmp_path / "drafts").glob("*.md"))
    assert len(draft.read_text(encoding="utf-8")) == 30
    assert "truncated" in result


@pytest.mark.asyncio
async def test_draft_rejects_empty_task(tmp_path: Path) -> None:
    llm = FakeLLM()
    handler = make_draft_content_handler(llm, tmp_path / "drafts")

    result = await handler(task="   ", persona="p")

    assert result.startswith("[error]")
    assert llm.calls == []


@pytest.mark.asyncio
async def test_draft_llm_failure_returns_error(tmp_path: Path) -> None:
    llm = FakeLLM(chat_responses=[RuntimeError("api down")])
    handler = make_draft_content_handler(llm, tmp_path / "drafts")

    result = await handler(task="t", persona="p")

    assert result.startswith("[error]")
    assert "api down" in result


def test_schema_shape() -> None:
    props = DRAFT_CONTENT_SCHEMA["properties"]
    assert set(props) >= {"task", "persona", "prefill", "max_chars"}
    assert DRAFT_CONTENT_SCHEMA["required"] == ["task", "persona"]


class _RecordingLLM:
    def __init__(self) -> None:
        self.registered: dict[str, Any] = {}

    def register_local_function(
        self, name: str, handler: Any, schema: dict[str, Any], description: str
    ) -> None:
        self.registered[name] = {"handler": handler, "schema": schema}


def test_register_draft_content_registers_tool(tmp_path: Path) -> None:
    llm = _RecordingLLM()

    register_draft_content(llm, tmp_path / "drafts")

    assert "DraftContent" in llm.registered
    assert llm.registered["DraftContent"]["schema"] is DRAFT_CONTENT_SCHEMA
