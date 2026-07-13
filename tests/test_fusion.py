"""Tests for ui_detector.fusion — OCR/YOLO SoM annotation fusion.

Reconciliation rule (boxes sorted by score descending):
  - IoU > 15%  -> merge: union bbox, combined content, higher score survives
  - IoU > 5%   -> dedup: drop the lower-score candidate
  - otherwise  -> both kept
"""

from __future__ import annotations

import pytest

from ui_detector.fusion import fuse_annotations


def _yolo(x1, y1, x2, y2, score=0.9):
    """YOLO-style annotation dict (normalized coords), as detect() emits."""
    return {
        "label": 0,
        "center_x": (x1 + x2) / 2,
        "center_y": (y1 + y2) / 2,
        "bbox": [x1, y1, x2, y2],
        "score": score,
    }


def _ocr(x1, y1, x2, y2, text, score=0.99, size=(1000, 1000)):
    """OCR entry in PIXEL coords of a `size` image."""
    return {"bbox": [x1, y1, x2, y2], "text": text, "score": score}, size


# ---------------------------------------------------------------------------
# Merge: IoU > 15%
# ---------------------------------------------------------------------------

def test_merge_ocr_text_into_overlapping_yolo_box():
    # YOLO (0.1,0.1)-(0.3,0.3) and OCR pixel (120,120)-(280,280) on a 1000px
    # image (= 0.12-0.28 normalized): nested inside the YOLO box, IoU ~0.64.
    ocr, size = _ocr(120, 120, 280, 280, "搜索")
    anns = fuse_annotations([ocr], size, [_yolo(0.1, 0.1, 0.3, 0.3, 0.9)], (1000, 1000))

    assert len(anns) == 1
    a = anns[0]
    assert a["text"] == "搜索"      # OCR content carried onto the marker
    assert a["icon"] is True        # YOLO detection backs it
    assert a["score"] == pytest.approx(0.99)  # OCR outscored YOLO


def test_merge_produces_union_bbox():
    ocr, size = _ocr(0, 0, 150, 150, "AB")          # (0,0)-(0.15,0.15)
    # YOLO (0.1,0.1)-(0.2,0.2): inter 0.05x0.05=0.0025, union 0.0225+0.01-0.0025
    # IoU = 0.0025/0.03 ~ 0.083 -> NOT a merge... use bigger overlap instead:
    yolo = _yolo(0.05, 0.05, 0.2, 0.2, 0.9)         # inter 0.1x0.1=0.01
    # union area = 0.0225+0.0225-0.01=0.035 -> IoU ~0.286 -> merge
    anns = fuse_annotations([ocr], size, [yolo], (1000, 1000))

    assert len(anns) == 1
    assert anns[0]["bbox"] == pytest.approx([0.0, 0.0, 0.2, 0.2])


def test_merge_keeps_higher_score_and_joins_distinct_texts():
    ocr1, size = _ocr(100, 100, 200, 200, "ALPHA", score=0.95)
    ocr2, _ = _ocr(110, 110, 210, 210, "BETA", score=0.90)  # IoU ~0.68 with ocr1
    anns = fuse_annotations([ocr1, ocr2], size, [], (1000, 1000))

    assert len(anns) == 1
    assert anns[0]["score"] == pytest.approx(0.95)
    assert anns[0]["text"] == "ALPHA BETA"


def test_merge_chains_through_growing_union():
    # A yolo (0,0)-(0.2,0.2); B ocr (0.05,0.05)-(0.25,0.25) merges into A
    # (IoU ~0.39); the union grows to (0,0)-(0.25,0.25). C ocr
    # (0.1,0.1)-(0.35,0.35) overlaps the GROWN union with IoU ~0.22 and merges
    # too — against A's ORIGINAL box its IoU was only ~0.11 (dedup zone).
    a = _yolo(0.0, 0.0, 0.2, 0.2, 0.9)
    b, size = _ocr(50, 50, 250, 250, "B")
    c, _ = _ocr(100, 100, 350, 350, "C", score=0.8)
    anns = fuse_annotations([b, c], size, [a], (1000, 1000))

    assert len(anns) == 1
    assert anns[0]["text"] == "B C"
    assert anns[0]["bbox"] == pytest.approx([0.0, 0.0, 0.35, 0.35])


# ---------------------------------------------------------------------------
# Dedup: 5% < IoU <= 15% keeps only the higher-score box
# ---------------------------------------------------------------------------

def test_mid_iou_overlap_keeps_only_higher_score():
    # A (0,0)-(0.2,0.2), B (0.15,0)-(0.35,0.2): inter 0.05*0.2=0.01,
    # union 0.04+0.04-0.01=0.07 -> IoU ~0.143 -> dedup, A (0.9) wins.
    anns = fuse_annotations(
        [], (1000, 1000),
        [_yolo(0.0, 0.0, 0.2, 0.2, 0.9), _yolo(0.15, 0.0, 0.35, 0.2, 0.7)],
        (1000, 1000),
    )

    assert len(anns) == 1
    assert anns[0]["score"] == pytest.approx(0.9)


def test_slight_overlap_below_5_percent_both_kept():
    # A (0,0)-(0.2,0.2), B (0.195,0)-(0.395,0.2): inter 0.005*0.2=0.001,
    # union 0.079 -> IoU ~0.013 -> both kept.
    anns = fuse_annotations(
        [], (1000, 1000),
        [_yolo(0.0, 0.0, 0.2, 0.2, 0.9), _yolo(0.195, 0.0, 0.395, 0.2, 0.7)],
        (1000, 1000),
    )

    assert len(anns) == 2


def test_nested_yolo_boxes_dedup_when_iou_between_5_and_15():
    # inner (0.05,0.05)-(0.1,0.1) inside outer (0,0)-(0.2,0.2):
    # IoU = 0.0025/0.04 = 0.0625 -> dedup, outer (0.9) survives.
    anns = fuse_annotations(
        [], (1000, 1000),
        [_yolo(0.0, 0.0, 0.2, 0.2, 0.9), _yolo(0.05, 0.05, 0.1, 0.1, 0.7)],
        (1000, 1000),
    )

    assert len(anns) == 1
    assert anns[0]["score"] == pytest.approx(0.9)


def test_nested_boxes_merge_when_iou_above_15():
    # inner (0.05,0.05)-(0.15,0.15) inside outer (0,0)-(0.2,0.2):
    # IoU = 0.01/0.04 = 0.25 -> merge into the outer box.
    anns = fuse_annotations(
        [], (1000, 1000),
        [_yolo(0.0, 0.0, 0.2, 0.2, 0.9), _yolo(0.05, 0.05, 0.15, 0.15, 0.7)],
        (1000, 1000),
    )

    assert len(anns) == 1
    assert anns[0]["bbox"] == pytest.approx([0.0, 0.0, 0.2, 0.2])


def test_tiny_box_inside_huge_box_kept_when_iou_low():
    # (0,0)-(1,1) vs (0.01,0.01)-(0.03,0.03): IoU ~ 0.0004 -> both kept.
    anns = fuse_annotations(
        [], (1000, 1000),
        [_yolo(0.0, 0.0, 1.0, 1.0, 0.9), _yolo(0.01, 0.01, 0.03, 0.03, 0.7)],
        (1000, 1000),
    )

    assert len(anns) == 2


def test_transitive_overlap_keeps_non_conflicting_boxes():
    # B overlaps A in the dedup zone (dropped); C disjoint -> A and C survive.
    anns = fuse_annotations(
        [], (1000, 1000),
        [
            _yolo(0.0, 0.0, 0.2, 0.2, 0.9),
            _yolo(0.15, 0.0, 0.35, 0.2, 0.8),   # IoU ~0.143 with A -> dropped
            _yolo(0.5, 0.5, 0.6, 0.6, 0.7),
        ],
        (1000, 1000),
    )

    assert [a["score"] for a in anns] == [pytest.approx(0.9), pytest.approx(0.7)]


# ---------------------------------------------------------------------------
# Coordinate handling and output contract
# ---------------------------------------------------------------------------

def test_ocr_boxes_rescaled_from_ocr_space_to_image_space():
    # OCR ran on a 500x500 image; the model sees 1000x1000 (e.g. UpgradeVision
    # original-resolution path). A 50px box at 500px space is 0.1 normalized —
    # NOT 0.05 — the OCR space, not the image space, defines the fraction.
    ocr, size = _ocr(0, 0, 50, 50, "X", size=(500, 500))
    anns = fuse_annotations([ocr], size, [], (1000, 1000))

    assert anns[0]["bbox"] == pytest.approx([0.0, 0.0, 0.1, 0.1])


def test_labels_renumbered_sequentially_by_score():
    anns = fuse_annotations(
        [], (1000, 1000),
        [
            _yolo(0.0, 0.0, 0.1, 0.1, 0.7),
            _yolo(0.2, 0.2, 0.3, 0.3, 0.95),
            _yolo(0.4, 0.4, 0.5, 0.5, 0.8),
        ],
        (1000, 1000),
    )

    assert [a["label"] for a in anns] == [1, 2, 3]
    assert anns[0]["score"] == pytest.approx(0.95)


def test_output_carries_visualizer_contract_fields():
    ocr, size = _ocr(10, 10, 60, 60, "OK")
    anns = fuse_annotations([ocr], size, [], (1000, 1000))

    a = anns[0]
    for key in ("label", "center_x", "center_y", "bbox", "score", "text", "icon"):
        assert key in a
    assert a["center_x"] == pytest.approx(0.035)
    assert a["icon"] is False  # OCR-only box


def test_yolo_only_box_has_no_text():
    anns = fuse_annotations([], (1000, 1000), [_yolo(0.1, 0.1, 0.2, 0.2, 0.9)], (1000, 1000))

    assert len(anns) == 1
    assert anns[0]["text"] is None
    assert anns[0]["icon"] is True


def test_empty_inputs_return_empty_list():
    assert fuse_annotations([], (1000, 1000), [], (1000, 1000)) == []


def test_malformed_entries_without_bbox_are_skipped():
    anns = fuse_annotations(
        [{"text": "no box", "score": 0.9}], (1000, 1000),
        [{"label": 2}, _yolo(0.1, 0.1, 0.2, 0.2, 0.8)],
        (1000, 1000),
    )

    assert len(anns) == 1
    assert anns[0]["icon"] is True
