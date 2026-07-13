"""Florence-2 icon captioning — the OmniParser ``icon_caption`` step.

YOLO (``icon_detect``) tells us WHERE the icons are but not WHAT they are
(its single class is literally "icon"). This module captions each detected
icon crop with a Florence-2 fine-tune (OmniParser's ``icon_caption``), so a
bare marker like ``[12] icon @(0.52,0.31)`` becomes
``[12] "magnifier" icon @(0.52,0.31)`` — the model can pick labels by
content instead of guessing from pixels.

The model loads lazily on the first caption (~1-2s, ~1GB weights); if CUDA
inference raises, the captioner falls back to CPU once and stays there.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger("caelum.ui_detector")

# Florence-2 task prompt: short image caption (the icon_caption fine-tune
# answers with terse UI descriptions like "magnifier" / "red close button").
_CAPTION_PROMPT = "<CAPTION>"

# Lazy transformers import holders — importing transformers pulls in torch
# and costs seconds, so it only happens on the first caption. Tests
# monkeypatch these attributes with fakes.
_FLORENCE_CLS = None
_PROCESSOR_CLS = None


def _get_classes():
    global _FLORENCE_CLS, _PROCESSOR_CLS
    if _FLORENCE_CLS is None or _PROCESSOR_CLS is None:
        from transformers import AutoModelForCausalLM, AutoProcessor

        _FLORENCE_CLS = AutoModelForCausalLM
        _PROCESSOR_CLS = AutoProcessor
    return _FLORENCE_CLS, _PROCESSOR_CLS


class IconCaptioner:
    """Florence-2 wrapper captioning YOLO-detected icon crops.

    ``caption_crops`` runs batched ``<CAPTION>`` generation (one caption per
    crop, in order). ``caption_markers`` is the perception-facing helper:
    bare icon markers (``icon=True`` with no OCR text) are cropped by their
    normalized bbox, captioned in score order (capped at ``max_icons`` per
    call to bound latency), and the caption is written into ``marker["text"]``.

    Florence-2 has no native transformers support as of 4.51: the model loads
    via ``AutoModelForCausalLM`` with ``trust_remote_code=True`` (the remote
    code is referenced by the checkpoint's auto_map). The OmniParser
    ``icon_caption`` checkpoint ships no processor files, so the processor
    loads from ``processor_path`` (default ``microsoft/Florence-2-base-ft``,
    which also hosts the remote modeling code) — the same split OmniParser
    itself uses.
    """

    def __init__(
        self,
        model_path: str | Path,
        device: str = "cuda:0",
        max_new_tokens: int = 20,
        batch_size: int = 8,
        processor_path: str = "microsoft/Florence-2-base-ft",
    ) -> None:
        self.model_path = str(model_path)
        self.device = device
        self.max_new_tokens = max_new_tokens
        self.batch_size = batch_size
        self.processor_path = processor_path
        self._model = None
        self._processor = None
        self._dtype = None
        self._fell_back = False

    def _load(self) -> None:
        if self._model is None:
            import torch

            model_cls, processor_cls = _get_classes()
            logger.info("Loading Florence-2 icon captioner from %s", self.model_path)
            self._dtype = (
                torch.float16 if str(self.device).startswith("cuda") else torch.float32
            )
            self._processor = processor_cls.from_pretrained(
                self.processor_path, trust_remote_code=True
            )
            self._model = model_cls.from_pretrained(
                self.model_path, torch_dtype=self._dtype, trust_remote_code=True
            ).to(self.device)

    def shutdown(self) -> None:
        """Release the loaded model (frees GPU memory); reloads on next use."""
        self._model = None
        self._processor = None

    def caption_crops(self, crops: list[Image.Image]) -> list[str]:
        """Caption each crop with ``<CAPTION>``; one caption per crop, in order."""
        if not crops:
            return []
        self._load()
        try:
            return self._caption_all(crops)
        except Exception:
            if str(self.device).startswith("cuda") and not self._fell_back:
                logger.warning(
                    "Florence-2 inference failed on %s; falling back to cpu",
                    self.device,
                )
                self.device = "cpu"
                self._fell_back = True
                self._model = self._model.to("cpu")
                return self._caption_all(crops)
            raise

    def _caption_all(self, crops: list[Image.Image]) -> list[str]:
        captions: list[str] = []
        for start in range(0, len(crops), self.batch_size):
            batch = crops[start : start + self.batch_size]
            inputs = self._processor(
                text=[_CAPTION_PROMPT] * len(batch),
                images=batch,
                return_tensors="pt",
                padding=True,
            ).to(self.device, self._dtype)
            ids = self._model.generate(
                pixel_values=inputs["pixel_values"],
                input_ids=inputs["input_ids"],
                max_new_tokens=self.max_new_tokens,
                num_beams=3,
                do_sample=False,
            )
            # OmniParser decodes with skip_special_tokens=True; short captions
            # are pad-filled up to max_new_tokens, so strip residual special
            # tokens defensively. Florence-2 answers "unanswerable" when it
            # can't caption a crop — treat that as no caption (a bare `icon`
            # marker is more honest than a misleading label).
            for text in self._processor.batch_decode(ids, skip_special_tokens=True):
                caption = str(text).replace("</s>", "").replace("<pad>", "").strip()
                captions.append("" if caption.lower() == "unanswerable" else caption)
        return captions

    def caption_markers(
        self, image: Image.Image, markers: list[dict], max_icons: int
    ) -> int:
        """Caption bare icon markers in place; returns how many were captioned.

        ``image`` is the model-visible screenshot the markers' normalized
        bboxes refer to. Markers are processed in score-descending order and
        capped at ``max_icons``; captions land in ``marker["text"]`` so the
        perception description lists them like OCR text.
        """
        targets = [m for m in markers if m.get("icon") and not m.get("text")]
        targets.sort(key=lambda m: m.get("score", 0.0), reverse=True)
        targets = targets[: max(0, max_icons)]
        if not targets:
            return 0
        w, h = image.size
        crops: list[Image.Image] = []
        owners: list[dict] = []
        for m in targets:
            x1, y1, x2, y2 = m["bbox"]
            box = (
                max(0, min(round(x1 * w), w)),
                max(0, min(round(y1 * h), h)),
                max(0, min(round(x2 * w), w)),
                max(0, min(round(y2 * h), h)),
            )
            if box[2] <= box[0] or box[3] <= box[1]:
                continue  # degenerate box: leave the marker uncaptioned
            crops.append(image.crop(box))
            owners.append(m)
        for marker, caption in zip(owners, self.caption_crops(crops)):
            if caption:
                marker["text"] = caption
        return len(owners)
