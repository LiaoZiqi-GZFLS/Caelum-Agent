"""End-to-end test of UpgradeVision: real screenshots before/after upgrade."""

import asyncio
import sys

from PIL import Image

from agent.config import load_config
from agent.perception import PerceptionModule


async def main() -> int:
    cfg = load_config()
    perception = PerceptionModule(cfg)

    # 1. default perception (720p cap)
    p1 = await perception.perceive(instruction="e2e default")
    w1, h1 = Image.open(p1.screenshot_path).size
    print(f"[default ] {p1.screenshot_path.name}: {w1}x{h1}")
    assert h1 <= cfg.screenshot.max_height, f"default exceeds 720p cap: {h1}"

    # 2. simulate UpgradeVision handler
    perception.max_size_override = (
        cfg.screenshot.upgraded_max_width, cfg.screenshot.upgraded_max_height,
    )
    p2 = await perception.perceive(instruction="e2e upgraded")
    w2, h2 = Image.open(p2.screenshot_path).size
    print(f"[upgraded] {p2.screenshot_path.name}: {w2}x{h2}")
    assert h2 <= cfg.screenshot.upgraded_max_height, f"upgrade exceeds 1080p cap: {h2}"
    assert (w2 * h2) > (w1 * h1), (
        f"upgraded image is not sharper: {w1}x{h1} -> {w2}x{h2} "
        "(is the physical screen resolution <= 720p?)"
    )

    # 3. reset (new-task behaviour)
    perception.max_size_override = None
    p3 = await perception.perceive(instruction="e2e reset")
    w3, h3 = Image.open(p3.screenshot_path).size
    print(f"[reset   ] {p3.screenshot_path.name}: {w3}x{h3}")
    assert h3 <= cfg.screenshot.max_height

    perception.shutdown()
    print(f"E2E OK: {w1}x{h1} -> {w2}x{h2} -> {w3}x{h3}")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
