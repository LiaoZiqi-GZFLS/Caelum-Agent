"""GUI-Actor-3B detector wrapper with a bounded visual-inference thread pool."""

from __future__ import annotations

import asyncio
import logging
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import torch
from PIL import Image
from transformers import AutoProcessor, AutoTokenizer

from agent.config import UIDetectorConfig
from ui_detector.gui_actor.inference import inference
from ui_detector.gui_actor.modeling_qwen25vl import (
    Qwen2_5_VLForConditionalGenerationWithPointer,
)
from ui_detector.verifier import UIVerifier
from ui_detector.visualizer import visualize_som

logger = logging.getLogger("caelum.ui_detector")


class UIDetector:
    """Wraps GUI-Actor-3B and runs heavy inference in a thread pool.

    The thread pool is capped at 2 workers so that at most two visual
    inferences run concurrently, matching the v8 design.
    """

    def __init__(self, config: UIDetectorConfig) -> None:
        self.config = config
        self.model: Qwen2_5_VLForConditionalGenerationWithPointer | None = None
        self.tokenizer: Any | None = None
        self.processor: Any | None = None
        self._executor = ThreadPoolExecutor(
            max_workers=2, thread_name_prefix="gui-actor"
        )
        self.verifier = UIVerifier(
            enabled=config.verifier.get("enabled", True),
            crop_size=config.verifier.get("crop_size", 224),
        )

    def load(self) -> None:
        model_path = Path(self.config.model_path).expanduser().resolve()
        dtype = getattr(torch, self.config.dtype)
        device_map = self.config.device if self.config.device != "cpu" else "cpu"
        if device_map.startswith("cuda") and not torch.cuda.is_available():
            import warnings

            warnings.warn(
                f"CUDA not available; falling back to CPU for GUI-Actor-3B. "
                f"This will be slow.",
                stacklevel=2,
            )
            device_map = "cpu"
            dtype = torch.float32

        self.model = Qwen2_5_VLForConditionalGenerationWithPointer.from_pretrained(
            model_path,
            torch_dtype=dtype,
            device_map=device_map,
            attn_implementation=self.config.attn_implementation,
            trust_remote_code=True,
        )
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_path, trust_remote_code=True
        )
        self.processor = AutoProcessor.from_pretrained(
            model_path, trust_remote_code=True
        )
        self.verifier.detector = self
        logger.info("Loaded GUI-Actor-3B from %s", model_path)

    def shutdown(self) -> None:
        """Release the inference thread pool."""
        self._executor.shutdown(wait=True)

    def predict(self, image: Image.Image, instruction: str, topk: int | None = None) -> dict[str, Any]:
        if self.model is None or self.processor is None or self.tokenizer is None:
            raise RuntimeError("UIDetector model is not loaded")

        topk = topk or self.config.topk
        conversation = [
            {
                "role": "system",
                "content": [
                    {
                        "type": "text",
                        "text": "You are a GUI agent. Given a screenshot and an instruction, locate the element.",
                    }
                ],
            },
            {
                "role": "user",
                "content": [
                    {"type": "image", "image": image},
                    {"type": "text", "text": instruction},
                ],
            },
        ]
        return inference(
            conversation,
            model=self.model,
            tokenizer=self.tokenizer,
            data_processor=self.processor,
            topk=topk,
        )

    async def predict_async(
        self, image: Image.Image, instruction: str, topk: int | None = None
    ) -> dict[str, Any]:
        """Run :meth:`predict` in the visual-inference thread pool."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            self._executor, self.predict, image, instruction, topk
        )

    async def annotate(
        self, image: Image.Image, instruction: str
    ) -> tuple[list[dict[str, Any]], int]:
        """Return (annotations, blocked_count).

        Annotations with verdict "reject" are excluded from the returned list.
        blocked_count is the number of rejected candidates.
        """
        pred = await self.predict_async(image, instruction)
        annotations = []
        points = pred.get("topk_points") or []
        values = pred.get("topk_values") or []
        for idx, (point_group, score) in enumerate(zip(points, values), start=1):
            if not point_group:
                continue
            xs = [p[0] for p in point_group]
            ys = [p[1] for p in point_group]
            annotations.append({
                "label": idx,
                "center_x": sum(xs) / len(xs),
                "center_y": sum(ys) / len(ys),
                "score": score,
                "normalized": True,
            })
        loop = asyncio.get_event_loop()
        verified = await loop.run_in_executor(
            self._executor, self.verifier.verify, image, instruction, annotations
        )
        passed = [a for a in verified if a.get("verdict") != "reject"]
        blocked = len(verified) - len(passed)
        return passed, blocked

    async def visualize(
        self,
        image: Image.Image,
        instruction: str,
        marker_radius: int = 12,
        font_size: int = 14,
    ) -> Image.Image:
        """Run detection and return the screenshot with SoM markers drawn."""
        annotations, _blocked = await self.annotate(image, instruction)
        return visualize_som(image, annotations, marker_radius, font_size)
