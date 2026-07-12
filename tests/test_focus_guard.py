"""Tests for agent.focus_guard (foreground focus watchdog)."""

from __future__ import annotations

import asyncio

import pytest

from agent.focus_guard import (
    FocusGuard,
    make_focus_guard_handler,
    register_focus_guard,
)


class _FakeWin32:
    """Scriptable win32 stand-in: a movable foreground + resolvable windows."""

    def __init__(self, windows: dict[str, int] | None = None) -> None:
        self.windows = windows or {}
        self.foreground = 0
        self.forced: list[int] = []
        self.force_errors = 0  # raise this many times before succeeding

    def find(self, title: str) -> int | None:
        for known, hwnd in self.windows.items():
            if known == title:
                return hwnd
        for known, hwnd in self.windows.items():
            if title.lower() in known.lower():
                return hwnd
        return None

    def get_foreground(self) -> int:
        return self.foreground

    def force(self, hwnd: int) -> None:
        if self.force_errors > 0:
            self.force_errors -= 1
            raise OSError("SetForegroundWindow failed")
        self.forced.append(hwnd)
        self.foreground = hwnd


def _guard(win32: _FakeWin32, interval: float = 0.01) -> FocusGuard:
    return FocusGuard(
        find_window=win32.find,
        get_foreground=win32.get_foreground,
        force_foreground=win32.force,
        interval=interval,
    )


async def _ticks(n: int = 3, interval: float = 0.01) -> None:
    for _ in range(n):
        await asyncio.sleep(interval)


@pytest.mark.asyncio
async def test_start_corrects_focus_drift():
    win32 = _FakeWin32({"微信": 100})
    guard = _guard(win32)
    await guard.start("微信")

    await _ticks()

    assert guard.active
    assert win32.forced.count(100) >= 1
    assert win32.foreground == 100
    await guard.stop()


@pytest.mark.asyncio
async def test_no_correction_when_focus_already_on_target():
    win32 = _FakeWin32({"微信": 100})
    win32.foreground = 100
    guard = _guard(win32)
    await guard.start("微信")

    await _ticks()

    assert win32.forced == []
    assert guard.corrections == 0
    await guard.stop()


@pytest.mark.asyncio
async def test_start_replaces_existing_guard():
    win32 = _FakeWin32({"微信": 100, "记事本": 200})
    guard = _guard(win32)
    await guard.start("微信")
    await guard.start("记事本")
    win32.foreground = 0

    await _ticks()

    assert win32.forced and all(h == 200 for h in win32.forced)
    assert guard.status()["title"] == "记事本"
    await guard.stop()


@pytest.mark.asyncio
async def test_start_unknown_window():
    win32 = _FakeWin32({})
    guard = _guard(win32)

    result = await guard.start("不存在")

    assert result.startswith("[error]")
    assert not guard.active


@pytest.mark.asyncio
async def test_stop_when_inactive_is_fine():
    guard = _guard(_FakeWin32())
    result = await guard.stop()
    assert "inactive" in result or "not" in result.lower()
    assert not guard.active


@pytest.mark.asyncio
async def test_force_errors_do_not_kill_the_loop():
    win32 = _FakeWin32({"微信": 100})
    win32.force_errors = 2
    guard = _guard(win32)
    await guard.start("微信")

    await _ticks(5)

    assert guard.active  # survived the errors
    assert win32.foreground == 100  # eventually corrected
    await guard.stop()
    assert not guard.active


@pytest.mark.asyncio
async def test_status_text():
    win32 = _FakeWin32({"微信": 100})
    guard = _guard(win32)
    assert "inactive" in guard.status_text()
    await guard.start("微信")
    text = guard.status_text()
    assert "微信" in text and "100" in text
    await guard.stop()


@pytest.mark.asyncio
async def test_handler_dispatch():
    win32 = _FakeWin32({"微信": 100})
    guard = _guard(win32)
    handler = make_focus_guard_handler(guard)

    assert (await handler(action="start")).startswith("[error]")  # no title
    assert "100" in await handler(action="start", title="微信")
    assert "active" in await handler(action="status")
    assert "stop" in (await handler(action="stop")).lower() or "inactive" in (
        await handler(action="status")
    )
    assert (await handler(action="bogus")).startswith("[error]")


def test_register_focus_guard_registers_tool():
    registered = {}

    class _StubLLM:
        def register_local_function(self, name, handler, schema=None, description=None):
            registered["name"] = name
            registered["schema"] = schema
            registered["description"] = description

    guard = register_focus_guard(_StubLLM())
    assert isinstance(guard, FocusGuard)
    assert registered["name"] == "FocusGuard"
    assert registered["schema"]["required"] == ["action"]
    assert "focus" in registered["description"].lower()
