"""Upload local images/videos to the Kimi Files API for native understanding.

Unlike ``file_reader`` (file-extract → text), this module uploads media with
``purpose=image`` / ``purpose=video`` and hands the model an ``ms://<file-id>``
reference, which Kimi renders natively as an ``image_url`` / ``video_url``
content part. The model never sees bytes — it sees the actual picture/video.

Constraints enforced here:
- 100 MB hard cap on every uploaded artifact.
- Images larger than 4K (3840x2160) are downscaled into the 4K box first.
- Videos are re-encoded with ffmpeg to 15fps / 1080p (downscale-only) before
  upload; the compressed file is cached by sha256 so re-uploads are free.

Uploaded files must stay alive while referenced in the conversation history,
so this module never deletes after upload; cleanup happens via
:meth:`MediaUploader.sweep_remote` at task end and startup.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
import re
import shutil
from pathlib import Path
from typing import Any

import httpx
from PIL import Image

logger = logging.getLogger("caelum.media")

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
VIDEO_EXTENSIONS = {
    ".mp4", ".mpeg", ".mpg", ".mov", ".avi", ".flv", ".webm", ".wmv",
    ".3gp", ".3gpp",
}

MAX_UPLOAD_BYTES = 100 * 1024 * 1024  # 100 MB hard cap for every upload
MAX_IMAGE_WIDTH = 3840   # 4K UHD bounding box
MAX_IMAGE_HEIGHT = 2160
VIDEO_FPS = 15
VIDEO_MAX_WIDTH = 1920   # 1080p = 1920x1080 bounding box

# Marker contract: the ViewMedia handler returns "[media_ref] <kind> <ms-url>"
# and the orchestrator lifts that reference into a real media content part.
MEDIA_REF_RE = re.compile(r"\[media_ref\] (image|video) (ms://\S+)")


def parse_media_refs(text: str) -> list[tuple[str, str]]:
    """Extract (kind, ms://url) pairs from a ViewMedia tool result."""
    return [(m.group(1), m.group(2)) for m in MEDIA_REF_RE.finditer(text or "")]


async def _ffmpeg_compress_video(src: Path, dst: Path) -> Path:
    """Re-encode a video to 15fps / <=1080p H.264 + AAC for upload."""
    ffmpeg = shutil.which("ffmpeg")
    if ffmpeg is None:
        raise RuntimeError(
            "ffmpeg not found: video upload requires ffmpeg for 15fps/1080p "
            "compression. Install ffmpeg and ensure it is on PATH."
        )
    dst.parent.mkdir(parents=True, exist_ok=True)
    proc = await asyncio.create_subprocess_exec(
        ffmpeg, "-y", "-i", str(src),
        "-vf", f"fps={VIDEO_FPS},scale={VIDEO_MAX_WIDTH}:-2:force_original_aspect_ratio=decrease",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "30",
        "-c:a", "aac", "-b:a", "96k",
        "-movflags", "+faststart",
        str(dst),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )
    _, stderr = await proc.communicate()
    if proc.returncode != 0:
        tail = stderr.decode("utf-8", errors="replace")[-500:]
        raise RuntimeError(f"ffmpeg compression failed: {tail}")
    return dst


class MediaUploader:
    """Upload images/videos and return ``ms://`` references for the model."""

    def __init__(
        self,
        base_url: str,
        api_key: str,
        work_dir: Path | str,
        http: Any | None = None,
        video_compressor: Any | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key
        self.work_dir = Path(work_dir)
        self._owns_http = http is None
        self.http = http if http is not None else httpx.AsyncClient(timeout=300.0)
        self._video_compressor = video_compressor or _ffmpeg_compress_video

    async def aclose(self) -> None:
        if self._owns_http:
            await self.http.aclose()

    async def upload(self, path: str | Path) -> tuple[str, str]:
        """Prepare and upload a local media file; return (kind, ms://url).

        Raises ValueError for unsupported types / oversized artifacts and
        FileNotFoundError for missing files; the tool handler formats these
        as "[error]".
        """
        p = Path(path).expanduser()
        suffix = p.suffix.lower()
        if suffix in IMAGE_EXTENSIONS:
            kind = "image"
        elif suffix in VIDEO_EXTENSIONS:
            kind = "video"
        else:
            raise ValueError(
                f"Unsupported media type '{suffix}'. ViewMedia handles images "
                f"({', '.join(sorted(IMAGE_EXTENSIONS))}) and videos "
                f"({', '.join(sorted(VIDEO_EXTENSIONS))})."
            )
        if not p.is_file():
            raise FileNotFoundError(f"File not found: {p}")

        artifact = self._prepare_image(p) if kind == "image" else await self._prepare_video(p)
        size = artifact.stat().st_size
        if size > MAX_UPLOAD_BYTES:
            raise ValueError(
                f"{kind.capitalize()} is {size / 1024 / 1024:.1f} MB after "
                "compression, exceeding the 100 MB upload limit."
            )
        file_id = await self._upload_file(artifact, kind)
        return kind, f"ms://{file_id}"

    def _prepare_image(self, path: Path) -> Path:
        """Downscale images larger than 4K into the 4K box; else return as-is."""
        with Image.open(path) as im:
            if im.width <= MAX_IMAGE_WIDTH and im.height <= MAX_IMAGE_HEIGHT:
                return path
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
            dst = self.work_dir / f"{digest}-4k.jpg"
            if dst.exists():
                return dst
            dst.parent.mkdir(parents=True, exist_ok=True)
            im = im.convert("RGB") if im.mode not in ("RGB", "L") else im.copy()
            im.thumbnail((MAX_IMAGE_WIDTH, MAX_IMAGE_HEIGHT))
            im.save(dst, format="JPEG", quality=88)
            logger.info(
                "Downscaled %s from %sx%s to %sx%s for upload",
                path.name, *Image.open(path).size, im.width, im.height,
            )
            return dst

    async def _prepare_video(self, path: Path) -> Path:
        """Compress a video to 15fps/1080p, cached by content sha256."""
        digest = hashlib.sha256(path.read_bytes()).hexdigest()[:8]
        dst = self.work_dir / f"{digest}-15fps.mp4"
        if dst.exists():
            logger.info("video compression cache hit: %s", path.name)
            return dst
        return await self._video_compressor(path, dst)

    async def _upload_file(self, path: Path, purpose: str) -> str:
        headers = {"Authorization": f"Bearer {self.api_key}"}
        with path.open("rb") as fh:
            resp = await self.http.post(
                f"{self.base_url}/files",
                headers=headers,
                data={"purpose": purpose},
                files={"file": (path.name, fh)},
            )
        resp.raise_for_status()
        return resp.json()["id"]

    async def sweep_remote(self) -> int:
        """Delete all leftover image/video uploads on the account.

        Media uploads are only referenced for the lifetime of one task, so
        once the task ends (or a new session starts) every image/video file
        on the account is stale. Returns the number deleted; never raises.
        """
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = await self.http.get(f"{self.base_url}/files", headers=headers)
            resp.raise_for_status()
            files = resp.json().get("data", [])
        except Exception as exc:
            logger.warning("Media sweep: listing remote files failed: %s", exc)
            return 0
        deleted = 0
        for entry in files:
            if entry.get("purpose") not in ("image", "video"):
                continue
            try:
                await self.http.delete(
                    f"{self.base_url}/files/{entry['id']}", headers=headers
                )
                deleted += 1
            except Exception as exc:
                logger.warning(
                    "Media sweep: failed to delete %s: %s", entry.get("id"), exc
                )
        if deleted:
            logger.info(
                "Media sweep: deleted %d leftover image/video upload(s)", deleted
            )
        return deleted


def make_view_media_handler(uploader: MediaUploader):
    """Build the async ViewMedia tool handler."""

    async def view_media(path: str) -> str:
        try:
            kind, url = await uploader.upload(path)
        except Exception as exc:
            return f"[error] {exc}"
        return (
            f"[media_ref] {kind} {url}\n"
            f"The {kind} has been attached to the conversation; you can now "
            "see it directly."
        )

    return view_media


VIEW_MEDIA_SCHEMA = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": (
                "Path to a local image (.png/.jpg/.jpeg/.webp/.gif) or video "
                "(.mp4/.mov/.avi/.webm/.wmv/...) file to look at."
            ),
        },
    },
    "required": ["path"],
}


def register_view_media(
    llm: Any, config: Any, cache_dir: Path | str
) -> MediaUploader | None:
    """Register the ViewMedia local function tool, if enabled in config.

    Returns the MediaUploader so the caller can schedule remote sweeps; None
    when the feature is disabled.
    """
    if not getattr(config, "enable_media_upload", True):
        return None
    http = getattr(llm, "http", None)
    uploader = MediaUploader(
        base_url=config.base_url,
        api_key=config.api_key,
        work_dir=Path(cache_dir) / "media",
        http=http,
    )
    llm.register_local_function(
        "ViewMedia",
        make_view_media_handler(uploader),
        schema=VIEW_MEDIA_SCHEMA,
        description=(
            "Look at a local image or video file. The file is uploaded and "
            "shown to you natively (you SEE the actual picture/video, not "
            "text). Images over 4K are downscaled; videos are compressed to "
            "15fps/1080p; the 100 MB upload limit applies. Use this for local "
            "media files — screens are already visible via perception."
        ),
    )
    return uploader
