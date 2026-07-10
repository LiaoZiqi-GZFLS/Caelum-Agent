"""Perception fusion: screenshot, OCR, UI tree, GUI-Actor SoM.

Blocking work (screenshots, compression, OCR) is offloaded to an IO thread
pool so the asyncio event loop stays responsive. Visual inference runs in the
UIDetector's own visual-inference thread pool.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import logging
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from PIL import Image

from agent.config import Config
from agent.snapshot_parser import parse_playwright_snapshot, parse_windows_snapshot, summarize_tree

if TYPE_CHECKING:
    from mcp_client import MCPMultiplexer
    from ui_detector import UIDetector


logger = logging.getLogger("caelum.perception")


@dataclass
class Perception:
    screenshot_path: Path
    description: str
    ocr_text: str
    ui_tree: dict[str, Any]
    som_annotations: list[dict[str, Any]]
    ui_hash: str = ""
    screen_width: int = 0
    screen_height: int = 0
    annotated_screenshot_path: Path | None = None
    blocked_count: int = 0


class PerceptionModule:
    def __init__(
        self,
        config: Config,
        mcp: MCPMultiplexer | None = None,
        ui_detector: UIDetector | None = None,
        max_io_workers: int = 8,
    ) -> None:
        self.config = config
        self.mcp = mcp
        self.ui_detector = ui_detector
        self._ocr: Any | None = None
        self._io_executor = ThreadPoolExecutor(
            max_workers=max_io_workers, thread_name_prefix="perception-io"
        )

    def shutdown(self) -> None:
        """Release the IO thread pool."""
        self._io_executor.shutdown(wait=True)

    async def perceive(self, instruction: str = "", with_vision: bool = False) -> Perception:
        cache_dir = self.config.cache_dir_absolute()
        cache_dir.mkdir(parents=True, exist_ok=True)
        timestamp = int(time.time() * 1000)
        screenshot_path = cache_dir / f"screenshot_{timestamp}.jpg"

        loop = asyncio.get_event_loop()
        image = await loop.run_in_executor(self._io_executor, self._capture_screenshot)
        orig_w, orig_h = image.size
        image_bytes = await loop.run_in_executor(
            self._io_executor, self._compress, image
        )
        await loop.run_in_executor(
            self._io_executor, screenshot_path.write_bytes, image_bytes
        )

        image_hash = await loop.run_in_executor(
            self._io_executor, self._compute_image_hash, image
        )
        ocr_text = await loop.run_in_executor(self._io_executor, self._run_ocr, image)
        ui_tree = await self._fetch_ui_tree()
        if with_vision:
            som_annotations, blocked_count = await self._run_ui_detector(image, instruction)
        else:
            som_annotations, blocked_count = [], 0
        # Drop placeholder/invalid annotations (e.g. detector error sentinels)
        # so visualize_som never crashes on a missing center_x/center_y.
        valid_annotations = [
            a for a in som_annotations
            if isinstance(a, dict) and "center_x" in a and "center_y" in a
        ]
        if len(valid_annotations) != len(som_annotations):
            logger.info(
                "Filtered %d invalid SoM annotation(s) before visualization",
                len(som_annotations) - len(valid_annotations),
            )
        som_annotations = valid_annotations

        ui_hash = self._compute_ui_hash(image_hash, ocr_text, ui_tree)
        description = self._build_description(ocr_text, ui_tree, som_annotations)

        annotated_screenshot_path: Path | None = None
        if som_annotations and self.ui_detector is not None:
            annotated_image = await loop.run_in_executor(
                self._io_executor, self._generate_annotated,
                screenshot_path, som_annotations,
            )
            annotated_screenshot_path = (
                cache_dir / f"screenshot_{timestamp}_annotated.jpg"
            )
            await loop.run_in_executor(
                self._io_executor,
                annotated_image.save,
                annotated_screenshot_path,
                "JPEG",
            )

        return Perception(
            screenshot_path=screenshot_path,
            description=description,
            ocr_text=ocr_text,
            ui_tree=ui_tree,
            som_annotations=som_annotations,
            ui_hash=ui_hash,
            screen_width=orig_w,
            screen_height=orig_h,
            annotated_screenshot_path=annotated_screenshot_path,
            blocked_count=blocked_count,
        )

    async def perceive_with_vision(self, instruction: str = "") -> Perception:
        """Capture perception with GUI-Actor SoM detection enabled.

        Used by ``DesktopInteract`` so labels are resolved against the latest
        screenshot. This is the only on-demand vision entry point.
        """
        return await self.perceive(instruction=instruction, with_vision=True)

    @staticmethod
    def _generate_annotated(
        screenshot_path: Path,
        som_annotations: list[dict[str, Any]],
    ) -> Image.Image:
        """Generate a SoM-annotated image from the compressed screenshot."""
        from ui_detector.visualizer import visualize_som

        compressed = Image.open(screenshot_path)
        return visualize_som(compressed, som_annotations)

    def _capture_screenshot(self) -> Image.Image:
        if self.config.screenshot.backend == "mss":
            import mss

            with mss.MSS() as sct:
                monitor = sct.monitors[0]
                raw = sct.grab(monitor)
                image = Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
        else:
            from PIL import ImageGrab

            image = ImageGrab.grab()

        if self.config.screenshot.crop_to_active_window:
            image = self._crop_to_active_window(image)
        return image

    def _crop_to_active_window(self, image: Image.Image) -> Image.Image:
        try:
            import win32gui
        except Exception:
            logger.debug("win32gui not available; skipping active-window crop")
            return image
        try:
            hwnd = win32gui.GetForegroundWindow()
            if not hwnd:
                return image
            rect = win32gui.GetWindowRect(hwnd)
            x1, y1, x2, y2 = rect
            sw, sh = image.size
            x1 = max(0, min(x1, sw))
            y1 = max(0, min(y1, sh))
            x2 = max(0, min(x2, sw))
            y2 = max(0, min(y2, sh))
            if x2 <= x1 or y2 <= y1:
                return image
            return image.crop((x1, y1, x2, y2))
        except Exception as exc:
            logger.warning("Failed to crop to active window: %s", exc)
            return image

    def _compress(self, image: Image.Image) -> bytes:
        max_size = (self.config.screenshot.max_width, self.config.screenshot.max_height)
        image.thumbnail(max_size)
        fmt = self.config.screenshot.format
        buf = io.BytesIO()
        if fmt == "PNG":
            image.save(buf, format="PNG")
        else:
            image.save(buf, format="JPEG", quality=self.config.screenshot.quality)
        return buf.getvalue()

    @staticmethod
    def _compute_image_hash(image: Image.Image) -> str:
        """Average-hash of a 16x16 grayscale screenshot."""
        gray = image.convert("L").resize((16, 16), Image.Resampling.LANCZOS)
        pixels = gray.tobytes()
        avg = sum(pixels) / len(pixels)
        bits = "".join("1" if p >= avg else "0" for p in pixels)
        return hex(int(bits, 2))[2:].zfill(64)

    @staticmethod
    def _compute_ui_hash(image_hash: str, ocr_text: str, ui_tree: dict[str, Any]) -> str:
        ocr_hash = hashlib.sha256(ocr_text.strip().lower().encode()).hexdigest()[:16]
        tree_hash = hashlib.sha256(
            str(ui_tree).encode("utf-8", errors="ignore")
        ).hexdigest()[:16]
        combined = f"{image_hash}|{ocr_hash}|{tree_hash}"
        return hashlib.sha256(combined.encode()).hexdigest()[:16]

    @staticmethod
    def has_changed(before: Perception, after: Perception) -> bool:
        """Return True if the UI state changed meaningfully between perceptions.

        A change in screenshot, OCR text, or UI tree indicates the last action
        likely had an effect on the desktop/browser state.
        """
        if not before.ui_hash or not after.ui_hash:
            # If hashes are unavailable, fall back to a conservative "changed".
            return True
        return before.ui_hash != after.ui_hash

    def _run_ocr(self, image: Image.Image) -> str:
        if not self.config.ocr.enabled:
            return ""
        if self._ocr is None:
            from rapidocr_onnxruntime import RapidOCR

            self._ocr = RapidOCR()
        with tempfile.NamedTemporaryFile(suffix=".jpg", delete=False) as tmp:
            image.save(tmp.name, format="JPEG", quality=self.config.screenshot.quality)
            tmp_path = tmp.name
        result = self._ocr(tmp_path)
        texts = []
        if result and isinstance(result, (list, tuple)):
            for item in result:
                if isinstance(item, (list, tuple)) and len(item) >= 2:
                    texts.append(str(item[1]))
        return "\n".join(texts)

    async def _fetch_ui_tree(self) -> dict[str, Any]:
        if self.mcp is None:
            return {}
        try:
            result = await self.mcp.call("windows", "Snapshot", {})
            if result.success and result.content:
                tree = parse_windows_snapshot(result.content)
                return {"snapshot": summarize_tree(tree)}
        except Exception as exc:
            logger.warning("Failed to fetch Windows UI tree: %s", exc)
        try:
            result = await self.mcp.call("playwright", "browser_snapshot", {})
            if result.success and result.content:
                tree = parse_playwright_snapshot(result.content)
                return {"snapshot": summarize_tree(tree)}
        except Exception as exc:
            return {"error": str(exc)}
        return {}

    async def _run_ui_detector(
        self, image: Image.Image, instruction: str
    ) -> tuple[list[dict[str, Any]], int]:
        if self.ui_detector is None or not self.config.ui_detector.enabled:
            return [], 0
        try:
            annotations, blocked = await self.ui_detector.annotate(image, instruction)
            return annotations, blocked
        except Exception as exc:
            logger.warning(
                "UI detector failed during annotate: %s", exc, exc_info=True
            )
            return [{"error": str(exc)}], 0

    @staticmethod
    def _build_description(
        ocr_text: str, ui_tree: dict[str, Any], som_annotations: list[dict[str, Any]]
    ) -> str:
        parts = ["Current screen:"]
        if ocr_text:
            parts.append(f"OCR text:\n{ocr_text}")
        if ui_tree:
            parts.append(f"UI tree:\n{str(ui_tree)[:2000]}")
        if som_annotations:
            parts.append(f"Detected elements: {len(som_annotations)}")
        return "\n\n".join(parts)
