r"""One-off: run YOLO detection + SoM annotation on a given image.

Two modes:

- default: direct YoloDetector.detect() on the raw image.
- --agent: the full production perception pipeline — a real PerceptionModule
  runs perceive() with only the mss screen grab replaced by the file, so OCR,
  inverse-DPI compression, YOLO-on-compressed, annotation filtering, and the
  model-facing description are exactly what the agent produces in a task.

Usage: .venv\Scripts\python.exe scripts/yolo_annotate.py [--agent] <image> [out.png]
"""

import asyncio
import sys
from pathlib import Path

sys.path.insert(0, ".")

from PIL import Image

from agent.config import load_config
from ui_detector.visualizer import visualize_som
from ui_detector.yolo_detector import YoloDetector


def _print_annotations(anns: list[dict]) -> None:
    for a in anns:
        b = a["bbox"]
        print(
            f"  [{a['label']:2d}] score={a['score']:.2f} "
            f"center=({a['center_x']:.3f},{a['center_y']:.3f}) "
            f"bbox=({b[0]:.3f},{b[1]:.3f},{b[2]:.3f},{b[3]:.3f})"
        )


def _direct(src: Path, out_path: Path) -> int:
    cfg = load_config()
    img = Image.open(src).convert("RGB")
    det = YoloDetector(
        cfg.yolo.model_path,
        device=cfg.yolo.device,
        conf=cfg.yolo.conf,
        imgsz=cfg.yolo.imgsz,
    )
    anns = det.detect(img)
    visualize_som(img, anns).save(out_path)
    print(f"image {img.size[0]}x{img.size[1]}, {len(anns)} annotations -> {out_path}")
    _print_annotations(anns)
    return 0


async def _agent(src: Path) -> int:
    from agent.perception import PerceptionModule

    cfg = load_config()
    det = YoloDetector(
        cfg.yolo.model_path,
        device=cfg.yolo.device,
        conf=cfg.yolo.conf,
        imgsz=cfg.yolo.imgsz,
    )
    cap = None
    if cfg.icon_caption.enabled:
        from ui_detector.icon_captioner import IconCaptioner

        cap = IconCaptioner(
            cfg.icon_caption.model_path,
            device=cfg.icon_caption.device,
            max_new_tokens=cfg.icon_caption.max_new_tokens,
            batch_size=cfg.icon_caption.batch_size,
            processor_path=cfg.icon_caption.processor_path,
        )
    pm = PerceptionModule(cfg, mcp=None, detector=det, captioner=cap)
    img = Image.open(src).convert("RGB")
    # Stand-in for the mss grab; everything downstream is production code.
    pm._capture_screenshot = lambda: img.copy()
    try:
        p = await pm.perceive()
    finally:
        pm.shutdown()
        det.shutdown()
        if cap is not None:
            cap.shutdown()

    print(
        f"pipeline: original {p.screen_width}x{p.screen_height} -> "
        f"model view {p.screenshot_width}x{p.screenshot_height}"
    )
    print(f"model-visible screenshot:  {p.screenshot_path}")
    print(f"model-visible annotated:   {p.annotated_screenshot_path}")
    print(f"annotations: {len(p.som_annotations)}")
    _print_annotations(p.som_annotations)
    print("--- description the model would receive ---")
    print(p.description)
    return 0


def main() -> int:
    args = [a for a in sys.argv[1:] if a != "--agent"]
    agent_mode = len(args) != len(sys.argv) - 1
    src = Path(args[0])

    if agent_mode:
        return asyncio.run(_agent(src))
    out_path = (
        Path(args[1]) if len(args) > 1 else Path("data/cache") / f"{src.stem}_som.png"
    )
    return _direct(src, out_path)


if __name__ == "__main__":
    sys.exit(main())
