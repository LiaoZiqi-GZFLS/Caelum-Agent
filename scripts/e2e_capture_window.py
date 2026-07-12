"""End-to-end test of CaptureWindow against the real Kimi API (manual smoke).

Needs a visible window titled (or containing) "微信" — e.g. the WeChat login
window. Captures it via PrintWindow, uploads, and asks the model what it sees.
"""

import asyncio
import sys
from pathlib import Path

from openai import OpenAI

from agent.config import load_config
from agent.media import MediaUploader
from agent.window_capture import WindowCapturer

WORK = Path("data/cache/captures_e2e")


async def main() -> int:
    title = sys.argv[1] if len(sys.argv) > 1 else "微信"
    cfg = load_config()
    uploader = MediaUploader(
        base_url=cfg.llm.base_url, api_key=cfg.llm.api_key, work_dir=WORK / "media"
    )
    capturer = WindowCapturer(WORK)

    full_title, path, rect = capturer.capture_by_title(title)
    print(f"[capture] '{full_title}' -> {path} (rect={rect})")
    kind, url = await uploader.upload(path)
    print(f"[upload] {kind} {url}")

    client = OpenAI(api_key=cfg.llm.api_key, base_url=cfg.llm.base_url)
    resp = client.chat.completions.create(
        model=cfg.llm.model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": "这张图里有什么？用一句话描述主要内容。"},
            ],
        }],
    )
    answer = resp.choices[0].message.content
    print(f"[vision] {answer}")

    deleted = await uploader.sweep_remote()
    print(f"[sweep] deleted {deleted} remote file(s)")
    await uploader.aclose()
    print("E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
