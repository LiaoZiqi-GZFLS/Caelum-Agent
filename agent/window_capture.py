"""CaptureWindow: grab a specific window via PrintWindow and show it to the model.

mss captures the composed desktop frame, which misses three kinds of windows:
display-affinity filtered ones (e.g. some WeChat dialogs), occluded ones, and
Qt/DirectComposition apps that paint outside GDI's reach. ``PrintWindow`` with
``PW_RENDERFULLCONTENT`` asks the window (or DWM) to render itself into our
bitmap instead, which covers all three.

The captured PNG is uploaded through the MediaUploader and returned as a
``[media_ref]`` marker, which the orchestrator lifts into a real image part —
the model sees the window contents directly.
"""

from __future__ import annotations

import ctypes
import hashlib
import logging
from ctypes import wintypes
from pathlib import Path
from typing import Any, Callable

logger = logging.getLogger("caelum.window_capture")

PW_RENDERFULLCONTENT = 0x2


def _win32_list_windows() -> list[tuple[int, str]]:
    """Enumerate visible top-level windows with non-empty titles."""
    user32 = ctypes.windll.user32
    results: list[tuple[int, str]] = []

    @ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    def cb(hwnd: int, lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd):
            return True
        length = user32.GetWindowTextLengthW(hwnd)
        if length == 0:
            return True
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        results.append((hwnd, buf.value))
        return True

    user32.EnumWindows(cb, 0)
    return results


def _win32_capture_window(hwnd: int, out: Path) -> tuple[int, int, int, int]:
    """Render a window into a PNG via PrintWindow(PW_RENDERFULLCONTENT).

    Returns the window's screen rect ``(left, top, width, height)`` — the
    captured bitmap is a 1:1 rendering of exactly that rect, so image pixel
    ``(ix, iy)`` maps to screen ``(left + ix, top + iy)``.
    """
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32
    if user32.IsIconic(hwnd):
        raise RuntimeError(
            "window is minimized; restore it first (windows__App can focus it)"
        )
    rect = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(rect))
    w, h = rect.right - rect.left, rect.bottom - rect.top
    if w <= 0 or h <= 0:
        raise RuntimeError(f"window has no visible area ({w}x{h})")

    hwnd_dc = user32.GetWindowDC(hwnd)
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bmp = gdi32.CreateCompatibleBitmap(hwnd_dc, w, h)
    gdi32.SelectObject(mem_dc, bmp)
    try:
        if not user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT):
            raise RuntimeError("PrintWindow failed (window refused to render)")

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
        bmi.bmiHeader.biHeight = -h  # top-down rows
        bmi.bmiHeader.biPlanes = 1
        bmi.bmiHeader.biBitCount = 32
        bmi.bmiHeader.biCompression = 0  # BI_RGB
        buf = ctypes.create_string_buffer(w * h * 4)
        gdi32.GetDIBits(mem_dc, bmp, 0, h, buf, ctypes.byref(bmi), 0)
        image = Image.frombuffer("RGBA", (w, h), buf, "raw", "BGRA", 0, 1).convert("RGB")
        out.parent.mkdir(parents=True, exist_ok=True)
        image.save(out)
    finally:
        gdi32.DeleteObject(bmp)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
    return (rect.left, rect.top, w, h)


class WindowCapturer:
    """Find a top-level window by title and capture it via PrintWindow.

    ``list_windows`` and ``capture`` are injectable for tests; the defaults
    make real win32 calls.
    """

    def __init__(
        self,
        out_dir: Path | str,
        list_windows: Callable[[], list[tuple[int, str]]] | None = None,
        capture: Callable[[int, Path], tuple[int, int, int, int] | None] | None = None,
    ) -> None:
        self.out_dir = Path(out_dir)
        self._list_windows = list_windows or _win32_list_windows
        self._capture = capture or _win32_capture_window

    def find(self, title: str) -> tuple[int, str] | None:
        """Resolve a title to (hwnd, full_title): exact match preferred."""
        windows = self._list_windows()
        lowered = title.lower()
        for hwnd, full in windows:
            if full.lower() == lowered:
                return hwnd, full
        for hwnd, full in windows:
            if lowered in full.lower():
                return hwnd, full
        return None

    def available_titles(self) -> list[str]:
        return [full for _, full in self._list_windows()]

    def capture_by_title(self, title: str) -> tuple[str, Path, tuple[int, int, int, int] | None]:
        """Capture the best-matching window; raise ValueError if not found.

        Returns ``(full_title, image_path, window_rect)`` where window_rect is
        ``(left, top, width, height)`` in screen space (None when the capture
        backend does not report it).
        """
        match = self.find(title)
        if match is None:
            titles = ", ".join(self.available_titles()[:20]) or "(none)"
            raise ValueError(
                f"No visible window matches '{title}'. Visible windows: {titles}"
            )
        hwnd, full = match
        digest = hashlib.sha256(f"{hwnd}|{full}".encode("utf-8")).hexdigest()[:8]
        out = self.out_dir / f"win-{digest}.png"
        rect = self._capture(hwnd, out)
        return full, out, rect


def make_capture_window_handler(
    capturer: WindowCapturer,
    uploader: Any,
    on_capture: Callable[[tuple[int, int, int, int], tuple[int, int]], None] | None = None,
):
    """Build the async CaptureWindow tool handler.

    ``on_capture(window_rect, image_size)`` (when provided and the capture
    reports a rect) lets the caller record the coordinate view so model-given
    loc values in the image's pixel space can be translated back to screen
    coordinates at click time.
    """

    async def capture_window(title: str) -> str:
        try:
            full_title, path, rect = capturer.capture_by_title(title)
            _, url = await uploader.upload(path)
        except Exception as exc:
            return f"[error] {exc}"
        message = (
            f"[media_ref] image {url}\n"
            f"Window '{full_title}' captured via PrintWindow and attached; "
            "you can now see its contents directly."
        )
        if rect is not None:
            from PIL import Image

            try:
                with Image.open(path) as img:
                    image_size = img.size
            except Exception:
                image_size = (rect[2], rect[3])
            if on_capture is not None:
                on_capture(rect, image_size)
            message += (
                f"\nThe window sits at screen rect (left={rect[0]}, top={rect[1]}, "
                f"size={rect[2]}x{rect[3]}); the image is a 1:1 rendering of it. "
                "Give loc coordinates in normalized [0,1] where "
                "(0,0)=top-left and (1,1)=bottom-right of this image. "
                "Conversion to screen coordinates is handled automatically."
            )
        return message

    return capture_window


CAPTURE_WINDOW_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {
            "type": "string",
            "description": (
                "Window title or a substring of it (e.g. '微信', '记事本'). "
                "Exact matches are preferred over substring matches."
            ),
        },
    },
    "required": ["title"],
}


def register_capture_window(
    llm: Any,
    config: Any,
    cache_dir: Path | str,
    uploader: Any | None,
    on_capture: Callable[[tuple[int, int, int, int], tuple[int, int]], None] | None = None,
) -> WindowCapturer | None:
    """Register the CaptureWindow tool; needs a MediaUploader for display."""
    if uploader is None or not getattr(config, "enable_media_upload", True):
        return None
    capturer = WindowCapturer(Path(cache_dir) / "captures")
    llm.register_local_function(
        "CaptureWindow",
        make_capture_window_handler(capturer, uploader, on_capture=on_capture),
        schema=CAPTURE_WINDOW_SCHEMA,
        description=(
            "Capture a specific window by title using PrintWindow and SEE its "
            "contents. Works for windows that are occluded, Qt/DirectComposition "
            "apps with no UIA tree, and display-affinity filtered windows that "
            "screenshots miss. Use when windows__Snapshot shows nothing useful "
            "inside an app, or the window is covered by others."
        ),
    )
    return capturer
