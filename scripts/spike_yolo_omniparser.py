"""Spike: OmniParser YOLO icon detection on a real screenshot.

Captures the primary monitor, runs the icon_detect YOLOv8 model (GPU if
available), draws numbered boxes OmniParser-style, and saves the annotated
image for visual inspection. Prints device, timing, and box count.

Run: .venv\\Scripts\\python.exe scripts/spike_yolo_omniparser.py
"""

from __future__ import annotations

import sys
import time

sys.path.insert(0, ".")

import mss
import torch
from PIL import Image, ImageDraw, ImageFont
from ultralytics import YOLO

MODEL = "models/omniparser/icon_detect/model.pt"
OUT = "data/cache/spike_yolo_annotated.png"


def main() -> int:
    with mss.MSS() as sct:
        shot = sct.grab(sct.monitors[0])
    image = Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
    print(f"screenshot: {image.size}", flush=True)

    device = "cuda:0" if torch.cuda.is_available() else "cpu"
    print(f"torch {torch.__version__}, cuda={torch.cuda.is_available()} -> device={device}", flush=True)

    model = YOLO(MODEL)
    # warmup + timed runs
    model.predict(image, imgsz=1280, conf=0.25, device=device, verbose=False)
    times = []
    results = None
    for _ in range(3):
        t0 = time.perf_counter()
        results = model.predict(image, imgsz=1280, conf=0.25, device=device, verbose=False)
        times.append(time.perf_counter() - t0)
    assert results is not None
    boxes = results[0].boxes
    print(f"inference: {[f'{t*1000:.0f}ms' for t in times]}", flush=True)
    print(f"boxes: {len(boxes)}", flush=True)

    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("arial.ttf", 22)
    except OSError:
        font = ImageFont.load_default()
    for i, box in enumerate(boxes, start=1):
        x1, y1, x2, y2 = box.xyxy[0].tolist()
        conf = float(box.conf[0])
        draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0), width=3)
        draw.rectangle([x1, y1 - 26, x1 + 34, y1], fill=(255, 0, 0))
        draw.text((x1 + 6, y1 - 25), str(i), fill=(255, 255, 255), font=font)
        if i <= 8:
            print(f"  [{i}] ({x1:.0f},{y1:.0f})-({x2:.0f},{y2:.0f}) conf={conf:.2f}", flush=True)

    image.save(OUT)
    print(f"saved: {OUT}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
