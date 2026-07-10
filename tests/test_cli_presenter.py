"""Tests for the rich, event-driven CLI presenter."""

from __future__ import annotations

import io

import pytest
from rich.console import Console

from agent.cli_presenter import CLIPresenter
from eventbus import EventBus
from eventbus.events import (
    LLMResponseReceived,
    ToolCallCompleted,
    ToolCallRequested,
)


def _make_presenter() -> tuple[CLIPresenter, io.StringIO]:
    buf = io.StringIO()
    console = Console(
        file=buf,
        force_terminal=True,
        color_system="truecolor",
        width=120,
    )
    return CLIPresenter(console=console), buf


@pytest.mark.asyncio
async def test_tool_requested_renders_arrow_and_name():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(ToolCallRequested(
        server="windows", tool_name="Click", arguments={"label": 5}
    ))

    out = buf.getvalue()
    assert "windows__Click" in out
    assert "▶" in out
    presenter.detach()


@pytest.mark.asyncio
async def test_tool_completed_success_renders_check():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(ToolCallCompleted(
        server="windows", tool_name="Click", result="OK: clicked", success=True
    ))

    out = buf.getvalue()
    assert "✓" in out
    assert "Click" in out
    assert "OK: clicked" in out
    presenter.detach()


@pytest.mark.asyncio
async def test_tool_completed_failure_renders_cross():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(ToolCallCompleted(
        server="windows", tool_name="Type", result="[error] no focus", success=False
    ))

    out = buf.getvalue()
    assert "✗" in out
    assert "Type" in out
    assert "no focus" in out
    presenter.detach()


@pytest.mark.asyncio
async def test_long_result_is_truncated_to_first_line():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    long = "line1\nline2\nline3"
    await bus.emit(ToolCallCompleted(
        server="fs", tool_name="read_file", result=long, success=True
    ))

    out = buf.getvalue()
    assert "line1" in out
    assert "line2" not in out  # only the first line is shown
    presenter.detach()


@pytest.mark.asyncio
async def test_llm_narration_printed_dim():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)

    await bus.emit(LLMResponseReceived(content="I will click the button.", tool_calls=[]))

    assert "I will click the button." in buf.getvalue()
    presenter.detach()


@pytest.mark.asyncio
async def test_narration_with_brackets_rendered_literally():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)
    await bus.emit(LLMResponseReceived(content="see [1] and [link](url) here", tool_calls=[]))
    out = buf.getvalue()
    assert "[1]" in out
    assert "[link](url)" in out
    presenter.detach()


def test_print_answer_contains_text():
    presenter, buf = _make_presenter()
    presenter.print_answer("The answer is **42**.")
    out = buf.getvalue()
    # Markdown keeps the literal text (bold markers become ANSI, not the word '**').
    assert "The answer is" in out
    assert "42" in out


def test_detach_stops_rendering():
    presenter, buf = _make_presenter()
    bus = EventBus()
    presenter.attach(bus)
    presenter.detach()
    # After detach, the bus no longer holds our handlers.
    import asyncio

    asyncio.run(
        bus.emit(ToolCallRequested(server="x", tool_name="y", arguments={}))
    )
    assert buf.getvalue() == ""
