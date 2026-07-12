"""FocusGuard: pin the foreground to a target window with a polling watchdog.

Windows blocks background processes from stealing the foreground with a plain
SetForegroundWindow (verified by scripts/spike_focus_guard.py on Win11); the
AttachThreadInput trick (join input queues with the current foreground thread
first) succeeds. All win32 calls are injectable for hermetic unit tests.

The guard is deliberately in-process (an asyncio task, not a subprocess): it
is cancelled with one ``await stop()``, dies with the agent, and needs no IPC.
"""

from __future__ import annotations

import asyncio
import ctypes
import logging
from typing import Any, Callable

from agent.window_capture import _win32_list_windows

logger = logging.getLogger("caelum.focus_guard")

SW_RESTORE = 9


def _win32_get_foreground() -> int:
    return int(ctypes.windll.user32.GetForegroundWindow())


def _win32_force_foreground(hwnd: int) -> None:
    """Level-2 recipe from the spike: AttachThreadInput + SetForegroundWindow."""
    user32 = ctypes.windll.user32
    fg = user32.GetForegroundWindow()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    cur_thread = ctypes.windll.kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur_thread, fg_thread, True)
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        user32.AttachThreadInput(cur_thread, fg_thread, False)


def _win32_find_window(title: str) -> int | None:
    """Exact title match first, then substring (same rule as CaptureWindow)."""
    windows = _win32_list_windows()
    for hwnd, known in windows:
        if known == title:
            return hwnd
    lowered = title.lower()
    for hwnd, known in windows:
        if lowered in known.lower():
            return hwnd
    return None


class FocusGuard:
    """Poll GetForegroundWindow and yank focus back to the target on drift."""

    def __init__(
        self,
        find_window: Callable[[str], int | None] = _win32_find_window,
        get_foreground: Callable[[], int] = _win32_get_foreground,
        force_foreground: Callable[[int], None] = _win32_force_foreground,
        interval: float = 0.4,
    ) -> None:
        self._find_window = find_window
        self._get_foreground = get_foreground
        self._force_foreground = force_foreground
        self._interval = interval
        self._task: asyncio.Task | None = None
        self._title: str | None = None
        self._hwnd: int | None = None
        self.corrections = 0

    @property
    def active(self) -> bool:
        return self._task is not None and not self._task.done()

    def status(self) -> dict[str, Any]:
        return {
            "active": self.active,
            "title": self._title,
            "hwnd": self._hwnd,
            "corrections": self.corrections,
            "interval": self._interval,
        }

    def status_text(self) -> str:
        st = self.status()
        if not st["active"]:
            return "[focus_guard] inactive"
        return (
            f"[focus_guard] active, target={st['title']!r} (hwnd={st['hwnd']}), "
            f"corrections={st['corrections']}, interval={st['interval']}s"
        )

    async def start(self, title: str) -> str:
        hwnd = self._find_window(title)
        if hwnd is None:
            return f"[error] No visible window titled or containing {title!r}."
        await self.stop()  # replace any existing guard
        self._title = title
        self._hwnd = hwnd
        self.corrections = 0
        self._task = asyncio.create_task(self._loop(), name="focus-guard")
        return (
            f"[focus_guard] watching {title!r} (hwnd={hwnd}); "
            f"focus will be pulled back every {self._interval}s until stopped."
        )

    async def stop(self) -> str:
        task, title = self._task, self._title
        self._task = None
        self._title = None
        self._hwnd = None
        if task is None or task.done():
            return "[focus_guard] was not active; nothing to stop."
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass
        return f"[focus_guard] stopped (was watching {title!r})."

    async def _loop(self) -> None:
        while True:
            try:
                # Re-resolve the hwnd each tick: if the window was closed and
                # reopened it has a new handle; if it is gone, idle.
                hwnd = self._find_window(self._title) if self._title else None
                if hwnd is not None:
                    self._hwnd = hwnd
                    if self._get_foreground() != hwnd:
                        self._force_foreground(hwnd)
                        self.corrections += 1
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                logger.debug("FocusGuard tick failed: %s", exc)
            await asyncio.sleep(self._interval)


FOCUS_GUARD_SCHEMA = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["start", "stop", "status"],
            "description": "start/stop the watchdog, or report its state.",
        },
        "title": {
            "type": "string",
            "description": (
                "Target window title or a substring of it (e.g. '微信', "
                "'记事本'). Required for action=start."
            ),
        },
    },
    "required": ["action"],
}


def make_focus_guard_handler(guard: FocusGuard):
    async def handler(action: str, title: str | None = None) -> str:
        action = (action or "").strip().lower()
        if action == "start":
            if not title or not title.strip():
                return "[error] title is required for action=start."
            return await guard.start(title.strip())
        if action == "stop":
            return await guard.stop()
        if action == "status":
            return guard.status_text()
        return "[error] action must be one of: start, stop, status"

    return handler


def register_focus_guard(llm: Any) -> FocusGuard:
    """Register the FocusGuard tool and return the guard (the orchestrator
    stops it automatically at task end)."""
    guard = FocusGuard()
    llm.register_local_function(
        "FocusGuard",
        make_focus_guard_handler(guard),
        schema=FOCUS_GUARD_SCHEMA,
        description=(
            "Pin the foreground focus to a target window. action=start begins "
            "a background watchdog that polls every ~0.4s and pulls the "
            "foreground back to the window with the given title whenever "
            "focus drifts; action=stop cancels it; action=status reports its "
            "state. Use ONLY right before a sequence of keyboard-dependent "
            "actions (Type, Shortcut) aimed at one window, and call stop as "
            "soon as the sequence is done — while active it will fight the "
            "user for focus. It is stopped automatically when the task ends."
        ),
    )
    return guard
