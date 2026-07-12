"""Repro/verify: concurrent DirectML sessions (RapidOCR + ChromaDB embedding).

Crash signature from the wild (Windows Application Error log):
  faulting module onnxruntime_pybind11_state.pyd, exception 0xc0000005,
  during the second task's perception while background skill learning
  (ChromaDB ONNX embedding, which also defaults to DML after the
  onnxruntime-directml swap) was likely in flight.

This script hammers two onnxruntime sessions from two threads:
  A: RapidOCR on a real screenshot (IO-thread role), always on DirectML
  B: ChromaDB collection upserts (skill-learning thread role)

Modes:
  --dml-embedding   OLD behavior: ChromaDB default EF (DmlExecutionProvider
                    first) -> reproduces the native 0xc0000005 crash.
  (default)         FIXED behavior: ChromaDB EF pinned to CPUExecutionProvider
                    (as in agent/memory.py) -> must survive 120s, exit 0.

Exit code 3221225477 (0xC0000005) = native crash reproduced.

Run: .venv\\Scripts\\python.exe scripts/repro_dml_crash.py [--dml-embedding]
"""

from __future__ import annotations

import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, ".")

# Pre-warm all heavy imports in the MAIN thread: concurrent first-imports of
# numpy/cv2 from two threads race on module initialization.
import chromadb  # noqa: F401
import cv2  # noqa: F401
import mss
import numpy  # noqa: F401
import onnxruntime as ort
from PIL import Image
from rapidocr_onnxruntime import RapidOCR  # noqa: F401

print(f"providers: {ort.get_available_providers()}", flush=True)

STOP = False
ERRORS: list[str] = []
COUNTS = {"ocr": 0, "chroma": 0}
DML_EMBEDDING = "--dml-embedding" in sys.argv


def ocr_worker(png_path: str) -> None:
    try:
        ocr = RapidOCR(det_use_dml=True, cls_use_dml=True, rec_use_dml=True)
        while not STOP:
            t0 = time.perf_counter()
            result = ocr(png_path)
            COUNTS["ocr"] += 1
            n = len(result) if result else 0
            print(f"[OCR ] iter {COUNTS['ocr']}: {time.perf_counter() - t0:.2f}s, {n} texts", flush=True)
    except Exception as exc:
        ERRORS.append(f"ocr worker died: {exc!r}")


def chroma_worker(tmp_dir: str) -> None:
    try:
        client = chromadb.PersistentClient(path=tmp_dir)
        if DML_EMBEDDING:
            coll = client.get_or_create_collection("repro")
        else:
            from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2

            coll = client.get_or_create_collection(
                "repro",
                embedding_function=ONNXMiniLM_L6_V2(
                    preferred_providers=["CPUExecutionProvider"]
                ),
            )
        i = 0
        while not STOP:
            i += 1
            t0 = time.perf_counter()
            coll.upsert(
                documents=[f"skill document number {i} about clicking buttons"],
                ids=[f"doc-{i}"],
            )
            coll.query(query_texts=["click the button"], n_results=1)
            COUNTS["chroma"] += 1
            print(f"[CHROMA] iter {i}: {time.perf_counter() - t0:.2f}s", flush=True)
    except Exception as exc:
        ERRORS.append(f"chroma worker died: {exc!r}")


def main() -> int:
    global STOP
    with mss.MSS() as sct:
        shot = sct.grab(sct.monitors[0])
    img = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    png = Path(tempfile.gettempdir()) / "repro_dml_shot.png"
    img.save(png, format="PNG")
    print(f"screenshot {img.size} -> {png}", flush=True)

    # Persistent dir: ChromaDB keeps chroma.sqlite3 open on Windows and
    # TemporaryDirectory cleanup would fail with WinError 32. Wipe stale
    # state first: switching EF between modes on an existing collection is
    # rejected by ChromaDB.
    import shutil

    tmp = Path("data/cache/repro-dml-chroma")
    shutil.rmtree(tmp, ignore_errors=True)
    tmp.mkdir(parents=True, exist_ok=True)
    a = threading.Thread(target=ocr_worker, args=(str(png),), daemon=True)
    b = threading.Thread(target=chroma_worker, args=(str(tmp),), daemon=True)
    a.start()
    b.start()
    deadline = time.time() + 120
    while time.time() < deadline and a.is_alive() and b.is_alive() and not ERRORS:
        time.sleep(1)
    STOP = True
    a.join(timeout=30)
    b.join(timeout=30)

    png.unlink(missing_ok=True)
    print(f"final counts: {COUNTS}", flush=True)
    if ERRORS:
        for e in ERRORS:
            print(f"WORKER ERROR: {e}", flush=True)
        print(f"iterations before failure: {COUNTS}", flush=True)
        return 2
    mode = "DML embedding (OLD, expected crash)" if DML_EMBEDDING else "CPU-pinned embedding (FIXED)"
    print(f"SURVIVED 120s [{mode}]: {COUNTS}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
