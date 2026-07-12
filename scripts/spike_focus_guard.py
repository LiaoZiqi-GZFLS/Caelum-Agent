"""Spike: can our process force another window to the foreground?

Windows normally blocks background processes from stealing the foreground
(SetForegroundWindow fails silently). This spike opens a Notepad window,
moves the foreground to our own console, then tries three escalation levels:

  1. naive SetForegroundWindow
  2. AttachThreadInput trick (join input queues with the current foreground
     thread, then SetForegroundWindow + BringWindowToTop)
  3. level 2 preceded by a benign Alt keypress (keybd_event) to earn
     foreground rights first

Each level is verified with GetForegroundWindow. The result tells us how the
FocusGuard tool must focus windows.
"""

from __future__ import annotations

import ctypes
import subprocess
import sys
import time

user32 = ctypes.windll.user32
kernel32 = ctypes.windll.kernel32

SW_RESTORE = 9
VK_MENU = 0x12
KEYEVENTF_KEYUP = 0x0002
WM_CLOSE = 0x0010


def get_foreground() -> int:
    return int(user32.GetForegroundWindow())


def window_title(hwnd: int) -> str:
    length = user32.GetWindowTextLengthW(hwnd)
    buf = ctypes.create_unicode_buffer(length + 1)
    user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value


def find_window_by_title(substring: str, timeout: float = 8.0) -> int:
    """Wait for a visible top-level window whose title contains substring.

    Win11's notepad.exe is a launcher stub: the real window belongs to the
    packaged Notepad process, so matching by pid does not work — match by
    title instead.
    """
    found = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:

        @ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
        def enum_cb(hwnd, _lparam):
            if not user32.IsWindowVisible(hwnd):
                return True
            n = user32.GetWindowTextLengthW(hwnd)
            if n == 0:
                return True
            buf = ctypes.create_unicode_buffer(n + 1)
            user32.GetWindowTextW(hwnd, buf, n + 1)
            if substring.lower() in buf.value.lower():
                found.append(int(hwnd))
                return False
            return True

        user32.EnumWindows(enum_cb, 0)
        if found:
            return found[0]
        time.sleep(0.3)
    return 0


def tap_alt() -> None:
    user32.keybd_event(VK_MENU, 0, 0, 0)
    user32.keybd_event(VK_MENU, 0, KEYEVENTF_KEYUP, 0)


def force_foreground_attach(hwnd: int) -> None:
    fg = get_foreground()
    fg_thread = user32.GetWindowThreadProcessId(fg, None)
    cur_thread = kernel32.GetCurrentThreadId()
    user32.AttachThreadInput(cur_thread, fg_thread, True)
    try:
        user32.ShowWindow(hwnd, SW_RESTORE)
        user32.SetForegroundWindow(hwnd)
        user32.BringWindowToTop(hwnd)
    finally:
        user32.AttachThreadInput(cur_thread, fg_thread, False)


def main() -> int:
    console = int(kernel32.GetConsoleWindow())
    print(f"console hwnd = {console} ({window_title(console)!r})")

    proc = subprocess.Popen(["notepad.exe"])
    notepad = find_window_by_title("Notepad")
    if not notepad:
        notepad = find_window_by_title("记事本")
    if not notepad:
        print("FAIL: could not find the Notepad window")
        proc.terminate()
        return 1
    print(f"notepad hwnd = {notepad} ({window_title(notepad)!r})")

    results = {}

    for level in (1, 2, 3):
        # Move the foreground to some other real window before each attempt.
        # (Our own console hwnd is a hidden conhost under Windows Terminal and
        # can never receive the foreground, so it makes a useless distractor.)
        distractor = find_window_by_title("设置") or find_window_by_title("文件资源管理器")
        if not distractor or distractor == notepad:
            print(f"[level {level}] no distractor window found")
            results[level] = None
            continue
        force_foreground_attach(distractor)
        time.sleep(0.4)
        fg = get_foreground()
        if fg == notepad:
            print(f"[level {level}] setup failed: notepad still foreground")
            results[level] = None
            continue

        if level == 1:
            user32.SetForegroundWindow(notepad)
        elif level == 2:
            force_foreground_attach(notepad)
        else:
            tap_alt()
            force_foreground_attach(notepad)
        time.sleep(0.4)

        ok = get_foreground() == notepad
        results[level] = ok
        names = {1: "naive SetForegroundWindow",
                 2: "AttachThreadInput",
                 3: "Alt-tap + AttachThreadInput"}
        print(f"[level {level}] {names[level]}: {'OK' if ok else 'FAILED'}")

    user32.PostMessageW(notepad, WM_CLOSE, 0, 0)
    time.sleep(0.5)
    proc.terminate()
    force_foreground_attach(console)

    print()
    if results.get(1):
        print("CONCLUSION: naive SetForegroundWindow works; no trick needed.")
        return 0
    if results.get(2) or results.get(3):
        best = 2 if results.get(2) else 3
        print(f"CONCLUSION: needs level {best} "
              f"({'AttachThreadInput' if best == 2 else 'Alt-tap + AttachThreadInput'}).")
        return 0
    print("CONCLUSION: foreground stealing is blocked at every level; "
          "FocusGuard cannot reliably refocus windows on this machine.")
    return 1


if __name__ == "__main__":
    sys.exit(main())
