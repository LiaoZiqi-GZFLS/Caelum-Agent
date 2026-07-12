"""Spike: capture display-affinity-filtered windows (e.g. WeChat login) via PrintWindow."""

import ctypes
import sys
from ctypes import wintypes
from pathlib import Path

from PIL import Image

user32 = ctypes.windll.user32
gdi32 = ctypes.windll.gdi32

WDA = {0x0: "NONE", 0x1: "MONITOR", 0x11: "EXCLUDEFROMCAPTURE"}
PW_RENDERFULLCONTENT = 0x2


def enum_visible_windows():
    results = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd, lparam):
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        affinity = wintypes.DWORD(0)
        ok = user32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity))
        results.append((hwnd, buf.value, affinity.value if ok else -1))
        return True

    user32.EnumWindows(cb, 0)
    return results


def capture_printwindow(hwnd: int, out: Path) -> tuple[int, int, int]:
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mem_dc, bmp)
    ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)

    class BITMAPINFOHEADER(ctypes.Structure):
        _fields_ = [
            ("biSize", wintypes.DWORD), ("biWidth", ctypes.c_long),
            ("biHeight", ctypes.c_long), ("biPlanes", wintypes.WORD),
            ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
            ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", ctypes.c_long),
            ("biYPelsPerMeter", ctypes.c_long), ("biClrUsed", wintypes.DWORD),
            ("biClrImportant", wintypes.DWORD),
        ]

    class BITMAPINFO(ctypes.Structure):
        _fields_ = [("bmiHeader", BITMAPINFOHEADER), ("bmiColors", wintypes.DWORD * 3)]

    bmi = BITMAPINFO()
    bmi.bmiHeader.biSize = ctypes.sizeof(BITMAPINFOHEADER)
    bmi.bmiHeader.biWidth = w
    bmi.bmiHeader.biHeight = -h  # top-down
    bmi.bmiHeader.biPlanes = 1
    bmi.bmiHeader.biBitCount = 32
    bmi.bmiHeader.biCompression = 0  # BI_RGB
    buf = ctypes.create_string_buffer(w * h * 4)
    gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
    image = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
    out.parent.mkdir(parents=True, exist_ok=True)
    image.save(out)

    gdi32.DeleteObject(bmp)
    gdi32.DeleteDC(mem_dc)
    user32.ReleaseDC(hwnd, hwnd_dc)
    return ok, w, h


def main() -> None:
    print("== visible windows with display affinity ==")
    targets = []
    for hwnd, title, affinity in enum_visible_windows():
        label = WDA.get(affinity, hex(affinity))
        marker = " <== FILTERED" if affinity != 0 else ""
        print(f"  hwnd={hwnd} affinity={label}{marker}  {title[:50]}")
        if affinity != 0:
            targets.append((hwnd, title))

    if not targets:
        print("\nNo affinity-filtered windows found. Open the WeChat login window and rerun.")
        return
    for hwnd, title in targets:
        out = Path(f"data/cache/_pw_{hwnd}.png")
        ok, w, h = capture_printwindow(hwnd, out)
        print(f"\nPrintWindow({title[:30]!r}) -> ok={ok} size={w}x{h} saved={out}")


if __name__ == "__main__":
    sys.exit(main())
