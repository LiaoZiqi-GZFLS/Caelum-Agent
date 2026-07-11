"""Tests for the msvcrt up/down choice menu."""

from __future__ import annotations

import io

from rich.console import Console

from agent.choice_menu import ask_choice


def _run(keys: list[str], options: list[str], question: str = "登录好了吗？"):
    """Drive ask_choice with a scripted key sequence; return (result, rendered)."""
    it = iter(keys)

    def fake_getch() -> str:
        try:
            return next(it)
        except StopIteration:
            raise AssertionError("menu asked for more keys than scripted")

    buf = io.StringIO()
    console = Console(file=buf, force_terminal=True, color_system="truecolor", width=120)
    result = ask_choice(question, options, console, getch=fake_getch)
    return result, buf.getvalue()


def test_enter_selects_first_option():
    result, _ = _run(["\r"], ["是，已完成登录", "否，我暂时无法完成登录"])
    assert result == "是，已完成登录"


def test_down_enter_selects_second_option():
    result, _ = _run(
        ["\xe0", "P", "\r"], ["是，已完成登录", "否，我暂时无法完成登录"]
    )
    assert result == "否，我暂时无法完成登录"


def test_typing_auto_jumps_to_type_row():
    result, out = _run(
        list("帮我点跳过") + ["\r"], ["是，已完成登录", "否，我暂时无法完成登录"]
    )
    assert result == "帮我点跳过"
    assert "帮我点跳过" in out


def test_backspace_deletes_char():
    result, _ = _run(["a", "b", "\x08", "c", "\r"], ["opt1", "opt2"])
    assert result == "ac"


def test_empty_type_row_enter_is_ignored_then_esc_cancels():
    # Two options -> index 2 is the type row. Enter on the empty type row must
    # not submit; the following ESC cancels.
    result, _ = _run(["\xe0", "P", "\xe0", "P", "\r", "\x1b"], ["opt1", "opt2"])
    assert result is None


def test_esc_cancels():
    result, _ = _run(["\x1b"], ["opt1", "opt2"])
    assert result is None


def test_ctrl_c_cancels():
    result, _ = _run(["\x03"], ["opt1", "opt2"])
    assert result is None
