"""Tests for agent.self_window (own console window control)."""

from __future__ import annotations

import pytest

from agent.self_window import (
    SW_HIDE,
    SW_MINIMIZE,
    SW_SHOW,
    ConsoleWindow,
    make_self_window_handler,
    register_self_window,
)


def _fake_win(hwnd=12345, visible=True, iconic=False, title="Caelum-Agent"):
    calls: list[tuple[int, int]] = []

    def shower(h: int, cmd: int) -> None:
        calls.append((h, cmd))

    win = ConsoleWindow(
        hwnd_getter=lambda: hwnd,
        shower=shower,
        is_visible=lambda h: visible,
        is_iconic=lambda h: iconic,
        get_title=lambda h: title,
    )
    return win, calls


def test_status_normal():
    win, _ = _fake_win()
    st = win.status()
    assert st == {
        "hwnd": 12345,
        "visible": True,
        "state": "normal",
        "title": "Caelum-Agent",
    }


def test_status_minimized():
    win, _ = _fake_win(iconic=True)
    assert win.status()["state"] == "minimized"


def test_status_hidden():
    win, _ = _fake_win(visible=False)
    assert win.status()["state"] == "hidden"


def test_status_no_console_when_hwnd_zero():
    win, _ = _fake_win(hwnd=0)
    st = win.status()
    assert st["state"] == "no_console"
    assert st["hwnd"] == 0


def test_hide_sends_sw_hide():
    win, calls = _fake_win()
    win.hide()
    assert calls == [(12345, SW_HIDE)]


def test_show_sends_sw_show():
    win, calls = _fake_win()
    win.show()
    assert calls == [(12345, SW_SHOW)]


def test_minimize_sends_sw_minimize():
    win, calls = _fake_win()
    win.minimize()
    assert calls == [(12345, SW_MINIMIZE)]


def test_show_is_noop_without_console():
    win, calls = _fake_win(hwnd=0)
    st = win.show()
    assert calls == []
    assert st["state"] == "no_console"


@pytest.mark.asyncio
async def test_handler_hide_returns_status_text():
    win, calls = _fake_win(visible=False)
    handler = make_self_window_handler(win)
    result = await handler(action="hide")
    assert calls == [(12345, SW_HIDE)]
    assert "hidden" in result
    assert "12345" in result


@pytest.mark.asyncio
async def test_handler_status_does_not_call_show_window():
    win, calls = _fake_win()
    handler = make_self_window_handler(win)
    result = await handler(action="status")
    assert calls == []
    assert "normal" in result


@pytest.mark.asyncio
async def test_handler_unknown_action():
    win, _ = _fake_win()
    handler = make_self_window_handler(win)
    result = await handler(action="explode")
    assert result.startswith("[error]")


def test_register_self_window_registers_tool():
    registered = {}

    class _StubLLM:
        def register_local_function(self, name, handler, schema=None, description=None):
            registered["name"] = name
            registered["schema"] = schema
            registered["description"] = description

    win = register_self_window(_StubLLM())
    assert isinstance(win, ConsoleWindow)
    assert registered["name"] == "SelfWindow"
    assert registered["schema"]["required"] == ["action"]
    assert "hide" in registered["description"]
