"""Spike: visualize GUI-Actor SoM annotations on a screenshot.

Run with synthetic annotations:
    python spikes/visualize_som.py

Run with the real GUI-Actor-3B model (slow on CPU):
    python spikes/visualize_som.py --real --instruction "click the start button"
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

from PIL import ImageGrab

from agent import load_config


def _capture_screenshot() -> Image.Image:
    return ImageGrab.grab()


def _synthetic_annotations(image):
    return [
        {"label": 1, "center_x": 0.25, "center_y": 0.25, "score": 0.95, "normalized": True},
        {"label": 2, "center_x": 0.50, "center_y": 0.50, "score": 0.88, "normalized": True},
        {"label": 3, "center_x": 0.75, "center_y": 0.75, "score": 0.72, "normalized": True},
    ]


async def main() -> int:
    parser = argparse.ArgumentParser(description="Visualize SoM annotations")
    parser.add_argument("--real", action="store_true", help="Use real GUI-Actor-3B model")
    parser.add_argument("--instruction", default="click the start button", help="Instruction for the detector")
    parser.add_argument("--output", default=None, help="Output image path")
    args = parser.parse_args()

    config = load_config()
    cache_dir = Path(config.cache_dir_absolute())
    cache_dir.mkdir(parents=True, exist_ok=True)

    print("Capturing screenshot...")
    image = _capture_screenshot()

    if args.real:
        from ui_detector import UIDetector

        print("Loading GUI-Actor-3B...")
        detector = UIDetector(config.ui_detector)
        detector.load()
        print(f"Running inference: {args.instruction!r}")
        try:
            annotated = await detector.visualize(image, args.instruction)
        finally:
            detector.shutdown()
    else:
        from ui_detector.visualizer import visualize_som

        print("Using synthetic annotations")
        annotations = _synthetic_annotations(image)
        annotated = visualize_som(image, annotations)

    out_path = args.output or cache_dir / f"som_visualization_{int(time.time() * 1000)}.jpg"
    annotated.save(out_path, quality=90)
    print(f"Saved annotated screenshot to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
