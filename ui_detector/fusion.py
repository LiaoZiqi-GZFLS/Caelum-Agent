"""Fuse OCR text boxes and YOLO icon detections into one SoM annotation list.

The two channels describe the same screen from different spaces: OCR boxes
are pixel rects of the OCR input image (inverse-DPI normalized, 1080p-capped),
YOLO detections are already normalized against the model-visible screenshot.
Fusion normalizes everything to [0, 1] of the model-visible image and
reconciles overlaps by IoU, processing boxes in score-descending order:

- IoU > 15%: same widget seen twice — MERGE into one marker: union bbox,
  concatenated OCR text, ``icon`` flag OR-ed, the higher score survives.
- IoU > 5%:  overlapping duplicates — keep only the higher-score box.
- otherwise:  both boxes live on as separate markers.

Output dicts carry the ``visualize_som`` contract (``label``, ``center_x``,
``center_y``, ``bbox``, ``score``) plus content: ``text`` (OCR string or
None) and ``icon`` (True when a YOLO detection backs the marker).
"""

from __future__ import annotations

# Reconciliation bands, ordered: merge beats dedup.
_MERGE_IOU_THRESHOLD = 0.15
_DEDUP_IOU_THRESHOLD = 0.05


def _box_area(b: list[float]) -> float:
    return max(0.0, b[2] - b[0]) * max(0.0, b[3] - b[1])


def _iou(a: list[float], b: list[float]) -> float:
    x1 = max(a[0], b[0])
    y1 = max(a[1], b[1])
    x2 = min(a[2], b[2])
    y2 = min(a[3], b[3])
    inter = max(0.0, x2 - x1) * max(0.0, y2 - y1)
    if inter <= 0:
        return 0.0
    union = _box_area(a) + _box_area(b) - inter
    return inter / union if union > 0 else 0.0


class _Entry:
    """Working record during fusion; converted to the output dict at the end."""

    __slots__ = ("score", "box", "text", "icon")

    def __init__(self, score: float, box: list[float], text: str | None, icon: bool) -> None:
        self.score = score
        self.box = box
        self.text = text
        self.icon = icon


def _merge_into(kept: _Entry, cand: _Entry) -> None:
    """Fold ``cand`` into the higher-score ``kept``: union box, joined text."""
    b, c = kept.box, cand.box
    kept.box = [min(b[0], c[0]), min(b[1], c[1]), max(b[2], c[2]), max(b[3], c[3])]
    if cand.text:
        if not kept.text:
            kept.text = cand.text
        elif cand.text not in kept.text:
            kept.text = f"{kept.text} {cand.text}"
    kept.icon = kept.icon or cand.icon


def fuse_annotations(
    ocr_boxes: list[dict],
    ocr_size: tuple[int, int],
    yolo_boxes: list[dict],
    image_size: tuple[int, int],
) -> list[dict]:
    """Reconcile OCR and YOLO boxes into a single SoM annotation list.

    ``ocr_boxes`` are ``{bbox: [x1, y1, x2, y2], text, score}`` in PIXELS of
    an ``ocr_size`` image; ``yolo_boxes`` are ``YoloDetector.detect()`` dicts
    (normalized ``bbox`` + ``score``). ``image_size`` is the model-visible
    screenshot size — it documents the output coordinate space; normalization
    itself uses ``ocr_size`` for OCR boxes while YOLO boxes arrive
    pre-normalized.

    Returns ``[{label, center_x, center_y, bbox, score, text, icon}]`` sorted
    by score descending with labels renumbered from 1.
    """
    entries: list[_Entry] = []
    ow, oh = ocr_size
    for item in ocr_boxes or []:
        bbox = item.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        x1, y1, x2, y2 = (float(v) for v in bbox[:4])
        box = [x1 / ow, y1 / oh, x2 / ow, y2 / oh] if ow > 0 and oh > 0 else [0.0] * 4
        entries.append(_Entry(float(item.get("score", 0.0)), box, item.get("text") or None, False))
    for ann in yolo_boxes or []:
        bbox = ann.get("bbox")
        if not bbox or len(bbox) < 4:
            continue
        entries.append(
            _Entry(float(ann.get("score", 0.0)), [float(v) for v in bbox[:4]], None, True)
        )

    entries.sort(key=lambda e: e.score, reverse=True)

    kept: list[_Entry] = []
    for cand in entries:
        resolved = False
        for k in kept:
            iou = _iou(cand.box, k.box)
            if iou > _MERGE_IOU_THRESHOLD:
                _merge_into(k, cand)
                resolved = True
                break
            if iou > _DEDUP_IOU_THRESHOLD:
                resolved = True  # dropped: lower score than every kept box
                break
        if not resolved:
            kept.append(cand)

    annotations = []
    for i, e in enumerate(kept, start=1):
        annotations.append(
            {
                "label": i,
                "center_x": (e.box[0] + e.box[2]) / 2,
                "center_y": (e.box[1] + e.box[3]) / 2,
                "bbox": e.box,
                "score": e.score,
                "text": e.text,
                "icon": e.icon,
            }
        )
    return annotations
