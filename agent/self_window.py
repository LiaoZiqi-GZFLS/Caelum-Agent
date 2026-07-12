"""Control the agent's own console window (hide/show/minimize/status).

Hiding the console during desktop operation keeps it out of screenshots and
the UIA tree and stops it from covering target apps. All win32 calls are
injectable so unit tests never touch a real console.
"""

from __future__ import annotations

import ctypes
from typing import Any, Callable

SW_HIDE = 0
SW_SHOW = 5
SW_MINIMIZE = 6

_NO_CONSOLE = {"hwnd": 0, "visible": False, "state": "no_console", "title": ""}


def _win32_console_hwnd() -> int:
    return int(ctypes.windll.kernel32.GetConsoleWindow())


def _win32_show_window(hwnd: int, cmd: int) -> None:
    ctypes.windll.user32.ShowWindow(hwnd, cmd)


def _win32_is_visible(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsWindowVisible(hwnd))


def _win32_is_iconic(hwnd: int) -> bool:
    return bool(ctypes.windll.user32.IsIconic(hwnd))


def _win32_window_title(hwnd: int) -> str:
    length = ctypes.windll.user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    ctypes.windll.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


class ConsoleWindow:
    """The agent process's own console window."""

    def __init__(
        self,
        hwnd_getter: Callable[[], int] = _win32_console_hwnd,
        shower: Callable[[int, int], None] = _win32_show_window,
        is_visible: Callable[[int], bool] = _win32_is_visible,
        is_iconic: Callable[[int], bool] = _win32_is_iconic,
        get_title: Callable[[int], str] = _win32_window_title,
    ) -> None:
        self._hwnd_getter = hwnd_getter
        self._shower = shower
        self._is_visible = is_visible
        self._is_iconic = is_iconic
        self._get_title = get_title

    def _hwnd(self) -> int:
        try:
            return int(self._hwnd_getter())
        except Exception:
            return 0

    def status(self) -> dict[str, Any]:
        hwnd = self._hwnd()
        if not hwnd:
            return dict(_NO_CONSOLE)
        visible = bool(self._is_visible(hwnd))
        iconic = bool(self._is_iconic(hwnd))
        state = "hidden" if not visible else ("minimized" if iconic else "normal")
        return {
            "hwnd": hwnd,
            "visible": visible,
            "state": state,
            "title": self._get_title(hwnd),
        }

    def _show(self, cmd: int) -> dict[str, Any]:
        hwnd = self._hwnd()
        if hwnd:
            self._shower(hwnd, cmd)
        return self.status()

    def hide(self) -> dict[str, Any]:
        return self._show(SW_HIDE)

    def show(self) -> dict[str, Any]:
        return self._show(SW_SHOW)

    def minimize(self) -> dict[str, Any]:
        return self._show(SW_MINIMIZE)


SELF_WINDOW_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["hide", "show", "minimize", "status"],
            "description": "What to do with the agent's own console window.",
        }
    },
    "required": ["action"],
}


def make_self_window_handler(win: ConsoleWindow):
    async def handler(action: str) -> str:
        action = (action or "").strip().lower()
        if action == "hide":
            st = win.hide()
        elif action == "show":
            st = win.show()
        elif action == "minimize":
            st = win.minimize()
        elif action == "status":
            st = win.status()
        else:
            return "[error] action must be one of: hide, show, minimize, status"
        return (
            f"Console window: {st['state']} "
            f"(hwnd={st['hwnd']}, title={st['title']!r})"
        )

    return handler


def register_self_window(llm: Any) -> ConsoleWindow:
    """Register the SelfWindow tool and return the ConsoleWindow handle (the
    orchestrator uses it for the auto-restore guardrails)."""
    win = ConsoleWindow()
    llm.register_local_function(
        "SelfWindow",
        make_self_window_handler(win),
        schema=SELF_WINDOW_SCHEMA,
        description=(
            "Control the agent's OWN console window. action=hide removes it "
            "from the screen so it does not appear in screenshots, clutter the "
            "UI tree, or cover target apps during desktop operation; "
            "action=show restores it; action=minimize minimizes it to the "
            "taskbar; action=status reports its current state. Hiding is "
            "always safe: the window is automatically restored when the task "
            "ends or human help is requested. Prefer hide over minimize when "
            "you need a clean screenshot."
        ),
    )
    return win
