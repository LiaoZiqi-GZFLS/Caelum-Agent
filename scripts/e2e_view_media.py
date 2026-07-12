"""End-to-end test of ViewMedia against the real Kimi API (not committed)."""

import asyncio
import shutil
import subprocess
import sys
from pathlib import Path

from openai import OpenAI
from PIL import Image, ImageDraw

from agent.config import load_config
from agent.media import MediaUploader

WORK = Path("data/cache/media_e2e")


def make_test_image(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    im = Image.new("RGB", (400, 300), "white")
    d = ImageDraw.Draw(im)
    d.rectangle([20, 20, 130, 130], fill="red")
    d.rectangle([150, 20, 260, 130], fill="green")
    d.rectangle([280, 20, 390, 130], fill="blue")
    im.save(path)


async def main() -> int:
    cfg = load_config()
    llm_cfg = cfg.llm
    client = OpenAI(api_key=llm_cfg.api_key, base_url=llm_cfg.base_url)
    uploader = MediaUploader(
        base_url=llm_cfg.base_url,
        api_key=llm_cfg.api_key,
        work_dir=WORK,
    )

    # --- 1. image upload + native understanding -------------------------
    img = WORK / "e2e.png"
    make_test_image(img)
    kind, url = await uploader.upload(img)
    print(f"[image] uploaded: kind={kind} url={url}")
    assert kind == "image"

    resp = client.chat.completions.create(
        model=llm_cfg.model,
        messages=[{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": url}},
                {"type": "text", "text": "图中从左到右有三个色块，分别是什么颜色？用一个词回答。"},
            ],
        }],
    )
    answer = resp.choices[0].message.content
    print(f"[image] model answer: {answer}")
    ok_img = all(c in answer.lower() for c in ("红", "绿", "蓝")) or all(
        c in answer.lower() for c in ("red", "green", "blue")
    )
    assert ok_img, f"image understanding failed: {answer}"

    # --- 2. video upload (only if ffmpeg available) ---------------------
    if shutil.which("ffmpeg"):
        raw_video = WORK / "e2e_raw.mp4"
        subprocess.run(
            ["ffmpeg", "-y", "-f", "lavfi", "-i",
             "color=c=orange:s=640x480:d=2",
             "-c:v", "libx264", "-pix_fmt", "yuv420p", str(raw_video)],
            check=True, capture_output=True,
        )
        kind, vurl = await uploader.upload(raw_video)
        print(f"[video] uploaded: kind={kind} url={vurl}")
        assert kind == "video"
        resp = client.chat.completions.create(
            model=llm_cfg.model,
            messages=[{
                "role": "user",
                "content": [
                    {"type": "video_url", "video_url": {"url": vurl}},
                    {"type": "text", "text": "这个视频画面的主色调是什么？用一个词回答。"},
                ],
            }],
        )
        vanswer = resp.choices[0].message.content
        print(f"[video] model answer: {vanswer}")
        assert ("橙" in vanswer) or ("orange" in vanswer.lower()), (
            f"video understanding failed: {vanswer}"
        )
    else:
        print("[video] ffmpeg not found, skipping video leg")

    # --- 3. sweep ---------------------------------------------------------
    deleted = await uploader.sweep_remote()
    print(f"[sweep] deleted {deleted} remote file(s)")
    await uploader.aclose()
    print("E2E OK")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
