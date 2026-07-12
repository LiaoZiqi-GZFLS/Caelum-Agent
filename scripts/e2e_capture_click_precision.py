"""E2E: CaptureWindow click precision against the real desktop (manual test).

The full coordinate chain, exercised for real:

1. Open a fresh Notepad with known three-line text (ALPHA / BRAVO / CHARLIE).
2. Capture its window via PrintWindow (the CaptureWindow path) -> image + rect.
3. Locate every line in the captured image with RapidOCR.
4. Translate each line's CENTER to screen coordinates EXACTLY like the
   orchestrator does after CaptureWindow (origin + scale from the window rect).
5. Click there for real via windows-mcp, paste a row-specific digit, then
   select-all + copy and check which row each digit landed in.

Gotcha: Win11 Notepad selects the whole line when you click at the line's
left edge / left margin, so the probe clicks the OCR box CENTER (the same
pointing style DesktopInteract uses), never the text start.

Do NOT move the mouse or switch windows while this runs.

Run: .venv\\Scripts\\python.exe scripts/e2e_capture_click_precision.py
"""

from __future__ import annotations

import asyncio
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image

from agent.config import MCPServerConfig, load_config
from agent.window_capture import WindowCapturer
from mcp_client import MCPClient

WORK = Path("data/cache/captures_e2e")
LINES = ["ALPHA", "BRAVO", "CHARLIE"]


def _notepad_pids() -> set[int]:
    out = subprocess.run(
        ["tasklist", "/fi", "imagename eq notepad.exe", "/fo", "csv", "/nh"],
        capture_output=True, text=True,
    ).stdout
    pids = set()
    for line in out.splitlines():
        parts = line.strip().strip('"').split('","')
        if len(parts) >= 2 and parts[1].isdigit():
            pids.add(int(parts[1]))
    return pids


def _wait_for_notepad(capturer: WindowCapturer, timeout: float = 10.0) -> tuple[int, str]:
    """Wait for a Notepad window and return (hwnd, title). The hwnd stays valid
    even as the title changes with the buffer contents."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        match = capturer.find("notepad") or capturer.find("记事本")
        if match is not None:
            return match
        time.sleep(0.3)
    raise RuntimeError("Notepad window did not appear")


def _grab(capturer: WindowCapturer, hwnd: int, name: str):
    """Capture one window by hwnd; returns (path, rect)."""
    out = WORK / f"{name}.png"
    rect = capturer._capture(hwnd, out)  # e2e script reaches into the backend
    return out, rect


def _snap(name: str) -> None:
    """Save an mss screenshot of the full desktop for visual diagnosis."""
    import mss

    out = WORK / name
    out.parent.mkdir(parents=True, exist_ok=True)
    with mss.mss() as sct:
        shot = sct.grab(sct.monitors[0])
    Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX").save(out)


async def main() -> int:
    print("*** Do not move the mouse or switch windows while this runs. ***")
    before = _notepad_pids()
    subprocess.Popen(["notepad.exe"])

    cfg = load_config()
    server = cfg.mcp_servers.model_dump()["windows"]
    client = MCPClient("windows", MCPServerConfig(**server), max_retries=2, base_delay=0.5)
    if not await client.connect():
        print("[fail] could not connect to windows-mcp")
        return 1

    new_pids: set[int] = set()
    try:
        capturer = WindowCapturer(WORK)
        hwnd, title = _wait_for_notepad(capturer)
        new_pids = _notepad_pids() - before
        print(f"[notepad] '{title}' hwnd={hwnd} (new pids: {sorted(new_pids)})")

        # First capture: learn the window rect so we can click the editor to
        # focus it (screen = rect origin + image point; the capture is 1:1).
        _, rect = _grab(capturer, hwnd, "notepad-empty")
        assert rect is not None, "capture did not report a window rect"
        left, top, w, h = rect
        await client.call("Click", {"loc": [left + w // 2, top + h // 2]})

        # Fresh buffer, then the known three lines via the clipboard (typing
        # simulation is unreliable against IME state; paste is atomic).
        await client.call("Shortcut", {"shortcut": "ctrl+a"})
        await client.call("Shortcut", {"shortcut": "delete"})
        await client.call("Clipboard", {"mode": "set", "text": "\r\n".join(LINES)})
        await client.call("Shortcut", {"shortcut": "ctrl+v"})
        await client.call("Wait", {"duration": 1})

        # CaptureWindow path: PrintWindow image + screen rect of the window.
        path, rect = _grab(capturer, hwnd, "notepad-text")
        assert rect is not None, "capture did not report a window rect"
        with Image.open(path) as img:
            iw, ih = img.size
        print(f"[capture] hwnd={hwnd} rect={rect} image={iw}x{ih}")

        # Locate each known line in the captured image.
        from rapidocr_onnxruntime import RapidOCR

        ocr = RapidOCR()
        result, _ = ocr(str(path))
        found = {text.strip().upper(): box for box, text, _score in (result or [])}
        hits_by_row = [(found[line], line) for line in LINES if line in found]
        if len(hits_by_row) != len(LINES):
            seen = [t for _, t, _s in (result or [])]
            print(f"[fail] OCR did not find all rows {LINES}; saw: {seen}")
            return 1
        print(f"[ocr] all {len(LINES)} rows located in the capture")
        left, top, w, h = rect

        # Probe: click each row's box CENTER in turn, insert a distinct digit,
        # then read the buffer once — the row each digit lands in shows the
        # real click-to-row mapping. The center matters: clicking a line's
        # left edge selects the whole line in Win11 Notepad, and the paste
        # would replace it (cascading row deletion, not coordinate drift).
        inserted = 0
        for row_idx, (box, _text) in enumerate(hits_by_row):
            xs = [p[0] for p in box]
            ys = [p[1] for p in box]
            ix = (min(xs) + max(xs)) / 2
            iy = (min(ys) + max(ys)) / 2
            sx = round(left + ix * w / iw)
            sy = round(top + iy * h / ih)
            print(f"[probe] row {row_idx} image ({ix:.0f},{iy:.0f}) -> screen ({sx},{sy})")
            click = await client.call("Click", {"loc": [sx, sy]})
            if not click.success:
                print(f"[fail] Click rejected: {click.content}")
                return 1
            _snap(f"click-row{row_idx}-after-click.png")
            await client.call("Clipboard", {"mode": "set", "text": str(row_idx)})
            await client.call("Shortcut", {"shortcut": "ctrl+v"})
            _snap(f"click-row{row_idx}-after-paste.png")
            inserted += 1

        await client.call("Shortcut", {"shortcut": "ctrl+a"})
        await client.call("Shortcut", {"shortcut": "ctrl+c"})
        clip = await client.call("Clipboard", {"mode": "get"})
        raw = (clip.content or "")
        for prefix in ("Clipboard content:",):
            if raw.startswith(prefix):
                raw = raw[len(prefix):]
        text = raw.replace("\r\n", "\n").strip()
        print(f"[buffer] {text!r}")

        lines = text.split("\n")
        ok = True
        for row_idx in range(inserted):
            landed = next(
                (i for i, ln in enumerate(lines) if str(row_idx) in ln), None
            )
            status = "OK" if landed == row_idx else f"DRIFT (landed in row {landed})"
            print(f"[verdict] aimed row {row_idx}: {status}")
            ok = ok and landed == row_idx
        return 0 if ok else 1
    finally:
        await client.disconnect()
        for pid in new_pids:
            subprocess.run(["taskkill", "/f", "/pid", str(pid)],
                           capture_output=True)
        print("[cleanup] killed the notepad instance we opened")


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
