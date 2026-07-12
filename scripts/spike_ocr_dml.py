"""Spike: RapidOCR on DirectML (GPU) vs CPU — speed and result parity.

Captures one real screenshot, then runs RapidOCR three times on CPU and
three times with all three models (det/cls/rec) on DmlExecutionProvider.
Reports per-run timings and the recognized text of both, so we can check
that DML results match CPU before wiring a config option.

Run: .venv\\Scripts\\python.exe scripts/spike_ocr_dml.py
"""

from __future__ import annotations

import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, ".")

import mss
import onnxruntime as ort
from PIL import Image
from rapidocr_onnxruntime import RapidOCR

print(f"onnxruntime {ort.__version__}, providers: {ort.get_available_providers()}")
assert "DmlExecutionProvider" in ort.get_available_providers(), "DML provider missing!"

with mss.MSS() as sct:
    shot = sct.grab(sct.monitors[0])
img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
print(f"screenshot: {img.size}")

with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
    img.save(tmp.name, format="PNG")
    png_path = tmp.name


def bench(label: str, **kwargs) -> list[str]:
    t0 = time.perf_counter()
    ocr = RapidOCR(**kwargs)
    t1 = time.perf_counter()
    print(f"\n[{label}] model load: {t1 - t0:.2f}s")
    texts: list[str] = []
    for i in range(3):
        t2 = time.perf_counter()
        result = ocr(png_path)
        t3 = time.perf_counter()
        texts = [str(r[1]) for r in result] if result else []
        print(f"[{label}] run {i + 1}: {t3 - t2:.2f}s, {len(texts)} texts")
    return texts


try:
    cpu_texts = bench("CPU")
    dml_texts = bench("DML", det_use_dml=True, cls_use_dml=True, rec_use_dml=True)

    print("\n=== parity check ===")
    cpu_set, dml_set = set(cpu_texts), set(dml_texts)
    print(f"CPU only: {sorted(cpu_set - dml_set)[:8]}")
    print(f"DML only: {sorted(dml_set - cpu_set)[:8]}")
    print(f"shared:   {len(cpu_set & dml_set)}/{max(len(cpu_set | dml_set), 1)}")
finally:
    Path(png_path).unlink(missing_ok=True)

print("\n=== done ===")
