"""End-to-end test of UpgradeVision: real screenshots before/after upgrade."""

import asyncio
import sys

sys.path.insert(0, ".")

from PIL import Image

from agent.config import load_config
from agent.perception import PerceptionModule, _display_scale, _ocr_resize_ratio


async def main() -> int:
    cfg = load_config()
    perception = PerceptionModule(cfg)

    # 1. default perception (inverse-DPI normalized, same as OCR input)
    p1 = await perception.perceive(instruction="e2e default")
    w1, h1 = Image.open(p1.screenshot_path).size
    native = (p1.screen_width, p1.screen_height)
    expected = _ocr_resize_ratio(native, _display_scale())
    print(f"[default ] {p1.screenshot_path.name}: {w1}x{h1} "
          f"(native {native[0]}x{native[1]}, scale {_display_scale()}, ratio {expected:.3f})")
    assert w1 == round(native[0] * expected) and h1 == round(native[1] * expected), (
        f"default screenshot does not match the OCR inverse-DPI rule: "
        f"expected ratio {expected:.3f} on {native}, got {w1}x{h1}"
    )

    # 2. simulate UpgradeVision handler: the original image (no resizing)
    perception.original_resolution = True
    p2 = await perception.perceive(instruction="e2e upgraded")
    w2, h2 = Image.open(p2.screenshot_path).size
    print(f"[upgraded] {p2.screenshot_path.name}: {w2}x{h2}")
    assert (w2, h2) == native, (
        f"upgrade must be the original image: expected {native}, got {w2}x{h2}"
    )

    # 3. reset (new-task behaviour)
    perception.original_resolution = False
    p3 = await perception.perceive(instruction="e2e reset")
    w3, h3 = Image.open(p3.screenshot_path).size
    print(f"[reset   ] {p3.screenshot_path.name}: {w3}x{h3}")
    assert (w3, h3) == (w1, h1), f"reset did not restore the default: {w3}x{h3} vs {w1}x{h1}"

    perception.shutdown()
    print(f"E2E OK: {w1}x{h1} -> {w2}x{h2} -> {w3}x{h3}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
