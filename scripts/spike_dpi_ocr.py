"""Spike: DPI awareness, mss capture size vs physical/logical resolution, OCR comparison.

Findings needed before implementing inverse-DPI OCR normalization:
1. Is our (python) process DPI-aware? (affects what mss captures)
2. Does mss capture physical or virtualized-logical pixels?
3. Does OCR improve with inverse-scale normalization on the captured image?

Run: .venv\\Scripts\\python.exe scripts/spike_dpi_ocr.py
"""

from __future__ import annotations

import ctypes
import io
import sys
import tempfile
from ctypes import wintypes
from pathlib import Path

sys.path.insert(0, ".")

user32 = ctypes.windll.user32
shcore = ctypes.windll.shcore

print("=== 1. Process DPI awareness ===")
try:
    awareness = ctypes.c_int()
    # GetProcessDpiAwareness: 0=UNAWARE, 1=SYSTEM_AWARE, 2=PER_MONITOR_AWARE
    hr = shcore.GetProcessDpiAwareness(None, ctypes.byref(awareness))
    names = {0: "UNAWARE (virtualized)", 1: "SYSTEM_AWARE", 2: "PER_MONITOR_AWARE"}
    print(f"GetProcessDpiAwareness -> {awareness.value} = {names.get(awareness.value)} (hr={hr})")
except Exception as exc:
    print(f"GetProcessDpiAwareness failed: {exc}")
print(f"IsProcessDPIAware -> {bool(user32.IsProcessDPIAware())}")

print("\n=== 2. Monitor scale / DPI (primary monitor) ===")
MONITOR_DEFAULTTOPRIMARY = 1
hmon = user32.MonitorFromPoint(wintypes.POINT(0, 0), MONITOR_DEFAULTTOPRIMARY)
scale = ctypes.c_int()
shcore.GetScaleFactorForMonitor(hmon, ctypes.byref(scale))
print(f"GetScaleFactorForMonitor -> {scale.value}%")

MDT_EFFECTIVE_DPI = 0
MDT_RAW_DPI = 2
for mode, name in ((MDT_EFFECTIVE_DPI, "EFFECTIVE"), (MDT_RAW_DPI, "RAW")):
    dx, dy = ctypes.c_uint(), ctypes.c_uint()
    try:
        shcore.GetDpiForMonitor(hmon, mode, ctypes.byref(dx), ctypes.byref(dy))
        print(f"GetDpiForMonitor({name}) -> {dx.value}x{dy.value} ({dx.value/96*100:.0f}%)")
    except Exception as exc:
        print(f"GetDpiForMonitor({name}) failed: {exc}")

print("\n=== 3. Screen sizes ===")
print(f"GetSystemMetrics(SM_CXSCREEN x SM_CYSCREEN) -> {user32.GetSystemMetrics(0)}x{user32.GetSystemMetrics(1)}  (virtualized if unaware)")

# Physical resolution via EnumDisplaySettings (unaffected by process awareness)
class DEVMODE(ctypes.Structure):
    _fields_ = [
        ("dmDeviceName", ctypes.c_wchar * 32),
        ("dmSpecVersion", ctypes.c_ushort),
        ("dmDriverVersion", ctypes.c_ushort),
        ("dmSize", ctypes.c_ushort),
        ("dmDriverExtra", ctypes.c_ushort),
        ("dmFields", ctypes.c_ulong),
        ("dmPosition_x", ctypes.c_long),
        ("dmPosition_y", ctypes.c_long),
        ("dmDisplayOrientation", ctypes.c_ulong),
        ("dmDisplayFixedOutput", ctypes.c_ulong),
        ("dmColor", ctypes.c_short),
        ("dmDuplex", ctypes.c_short),
        ("dmYResolution", ctypes.c_short),
        ("dmTTOption", ctypes.c_short),
        ("dmCollate", ctypes.c_short),
        ("dmFormName", ctypes.c_wchar * 32),
        ("dmLogPixels", ctypes.c_ushort),
        ("dmBitsPerPel", ctypes.c_ulong),
        ("dmPelsWidth", ctypes.c_ulong),
        ("dmPelsHeight", ctypes.c_ulong),
        ("dmDisplayFlags", ctypes.c_ulong),
        ("dmDisplayFrequency", ctypes.c_ulong),
        ("dmICMMethod", ctypes.c_ulong),
        ("dmICMIntent", ctypes.c_ulong),
        ("dmMediaType", ctypes.c_ulong),
        ("dmDitherType", ctypes.c_ulong),
        ("dmReserved1", ctypes.c_ulong),
        ("dmReserved2", ctypes.c_ulong),
        ("dmPanningWidth", ctypes.c_ulong),
        ("dmPanningHeight", ctypes.c_ulong),
    ]

devmode = DEVMODE()
devmode.dmSize = ctypes.sizeof(DEVMODE)
ENUM_CURRENT_SETTINGS = -1
if user32.EnumDisplaySettingsW(None, ENUM_CURRENT_SETTINGS, ctypes.byref(devmode)):
    print(f"EnumDisplaySettings (physical) -> {devmode.dmPelsWidth}x{devmode.dmPelsHeight}")

print("\n=== 4. mss capture size ===")
import mss

with mss.mss() as sct:
    for i, mon in enumerate(sct.monitors):
        print(f"monitor[{i}]: {mon}")
    shot = sct.grab(sct.monitors[0])
    from PIL import Image

    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    print(f"captured image size: {image.size}")

print("\n=== 5. OCR comparison: raw vs inverse-scale vs 1080p-cap ===")
scale_factor = scale.value / 100.0
w, h = image.size
variants = {"raw": image}
if scale_factor > 1.0:
    variants[f"inverse-scale(x{1/scale_factor:.3f})"] = image.resize(
        (round(w / scale_factor), round(h / scale_factor)), Image.Resampling.LANCZOS
    )
capped = image.copy()
capped.thumbnail((1920, 1080))
variants[f"1080p-cap({capped.width}x{capped.height})"] = capped

from rapidocr_onnxruntime import RapidOCR

ocr = RapidOCR()
for name, img in variants.items():
    with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
        img.save(tmp.name, format="PNG")
        tmp_path = tmp.name
    try:
        result = ocr(tmp_path)
    finally:
        Path(tmp_path).unlink(missing_ok=True)
    n = len(result) if result else 0
    sample = " | ".join(str(r[1]) for r in result[:6]) if result else "(nothing)"
    print(f"[{name}] {img.size} -> {n} text(s): {sample[:120]}")

print("\n=== done ===")
