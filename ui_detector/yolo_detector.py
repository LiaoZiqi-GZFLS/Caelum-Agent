"""OmniParser icon-detection YOLO wrapper for SoM annotation.

Wraps the OmniParser v2 ``icon_detect`` YOLOv8 fine-tune (~40MB) as the
vision grounding backend: given a PIL image it returns SoM-style annotations
(numbered boxes with normalized centers) that ``visualize_som`` draws and
``DesktopInteract(label=N)`` clicks.

Measured ~42ms/frame at 2560x1440 on an RTX 4090 Laptop — see
scripts/spike_yolo_omniparser.py.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PIL import Image

logger = logging.getLogger("caelum.ui_detector")

# Lazy ultralytics import holder — importing ultralytics pulls in torch and
# costs seconds, so it only happens on the first detection. Tests monkeypatch
# this attribute with a fake YOLO class.
_YOLO = None


def _get_yolo_cls():
    global _YOLO
    if _YOLO is None:
        from ultralytics import YOLO

        _YOLO = YOLO
    return _YOLO


class YoloDetector:
    """OmniParser icon_detect YOLOv8 wrapper returning SoM-style annotations.

    ``detect()`` returns dicts shaped for ``visualize_som`` /
    ``DesktopInteract``: ``{label, center_x, center_y, bbox, score}`` with all
    coordinates normalized to [0, 1] against the input image. Labels start at
    1 and follow confidence-descending order.

    Overlapping boxes are NOT reconciled here — that happens downstream in
    ``ui_detector.fusion.fuse_annotations``, which merges/dedups YOLO and OCR
    boxes uniformly.

    The model loads lazily on the first ``detect()``. If CUDA inference
    raises, the detector falls back to CPU once and stays there.
    """

    def __init__(
        self,
        model_path: str | Path,
        device: str = "cuda:0",
        conf: float = 0.25,
        imgsz: int = 1280,
    ) -> None:
        self.model_path = str(model_path)
        self.device = device
        self.conf = conf
        self.imgsz = imgsz
        self._model = None
        self._fell_back = False

    def _load(self):
        if self._model is None:
            logger.info("Loading YOLO icon detector from %s", self.model_path)
            self._model = _get_yolo_cls()(self.model_path)
        return self._model

    def shutdown(self) -> None:
        """Release the loaded model (frees GPU memory); reloads on next use."""
        self._model = None

    def detect(self, image: Image.Image) -> list[dict]:
        model = self._load()
        try:
            results = model.predict(
                image,
                imgsz=self.imgsz,
                conf=self.conf,
                device=self.device,
                verbose=False,
            )
        except Exception:
            if self.device.startswith("cuda") and not self._fell_back:
                logger.warning(
                    "YOLO inference failed on %s; falling back to cpu", self.device
                )
                self.device = "cpu"
                self._fell_back = True
                results = model.predict(
                    image,
                    imgsz=self.imgsz,
                    conf=self.conf,
                    device="cpu",
                    verbose=False,
                )
            else:
                raise
        w, h = image.size
        boxes = []
        for box in results[0].boxes:
            x1, y1, x2, y2 = box.xyxy[0].tolist()
            boxes.append((float(box.conf[0]), x1, y1, x2, y2))
        boxes.sort(key=lambda b: b[0], reverse=True)
        annotations = []
        for i, (score, x1, y1, x2, y2) in enumerate(boxes, start=1):
            annotations.append(
                {
                    "label": i,
                    "center_x": ((x1 + x2) / 2) / w,
                    "center_y": ((y1 + y2) / 2) / h,
                    "bbox": [x1 / w, y1 / h, x2 / w, y2 / h],
                    "score": score,
                }
            )
        return annotations
