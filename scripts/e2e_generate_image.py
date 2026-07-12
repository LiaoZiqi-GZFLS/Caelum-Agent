"""End-to-end test of GenerateImage against the real Kimi API (manual smoke)."""

import asyncio
import sys
from pathlib import Path

from agent.config import load_config
from agent.image_gen import ImageGenerator
from agent.llm_client import LLMClient
from agent.media import MediaUploader

WORK = Path("data/cache/generated_e2e")


async def main() -> int:
    cfg = load_config()
    llm = LLMClient(cfg.llm)
    await llm.initialize()
    uploader = MediaUploader(
        base_url=cfg.llm.base_url,
        api_key=cfg.llm.api_key,
        work_dir=WORK / "media",
    )
    generator = ImageGenerator(llm, uploader, WORK)

    result = await generator.generate(
        "一只坐在笔记本电脑前的橘猫，扁平插画风格，蓝色背景，"
        "电脑屏幕上显示代码，画面简洁可爱"
    )
    print(f"rounds={result['rounds']} ok={result['ok']} issues={result['issues']!r}")
    print(f"path={result['path']}")

    deleted = await uploader.sweep_remote()
    print(f"[sweep] deleted {deleted} remote file(s)")
    await uploader.aclose()
    await llm.close()

    assert result["path"] is not None and Path(result["path"]).exists()
    print("E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
