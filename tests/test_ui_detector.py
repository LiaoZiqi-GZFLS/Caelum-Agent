"""Tests for ui_detector visualizer and verifier."""

from __future__ import annotations

from typing import Any

import pytest
from PIL import Image

from ui_detector.verifier import PASS_THRESHOLD, REJECT_THRESHOLD, UIVerifier, VerifierVerdict
from ui_detector.visualizer import visualize_som


def _make_image(size: tuple[int, int] = (100, 100)) -> Image.Image:
    return Image.new("RGB", size, color=(50, 50, 50))


def test_visualize_som_draws_markers():
    image = _make_image((200, 200))
    annotations = [
        {"label": 1, "center_x": 0.25, "center_y": 0.25, "score": 0.9, "normalized": True},
        {"label": 2, "center_x": 0.75, "center_y": 0.75, "score": 0.8, "normalized": True},
    ]
    result = visualize_som(image, annotations)
    assert result.size == image.size
    assert result.mode == "RGB"


def test_visualize_som_empty_annotations():
    image = _make_image()
    result = visualize_som(image, [])
    assert result.size == image.size


def test_visualize_som_pixel_coordinates():
    image = _make_image((200, 200))
    annotations = [
        {"label": "A", "center_x": 50, "center_y": 50, "score": 0.9, "normalized": False},
    ]
    result = visualize_som(image, annotations)
    assert result.size == image.size


class _FakeDetector:
    def __init__(self, responses: list[dict[str, Any]]) -> None:
        self._responses = responses
        self._index = 0

    def predict(self, image: Image.Image, instruction: str, topk: int | None = None) -> dict[str, Any]:
        response = self._responses[self._index % len(self._responses)]
        self._index += 1
        return response


def test_verifier_disabled_keeps_detector_ordering():
    verifier = UIVerifier(enabled=False)
    image = _make_image()
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.5, "normalized": True},
        {"label": 2, "center_x": 0.5, "center_y": 0.5, "score": 0.9, "normalized": True},
    ]
    result = verifier.verify(image, "click ok", annotations)
    assert [r["label"] for r in result] == [2, 1]


def test_verifier_no_detector_falls_back_to_score():
    verifier = UIVerifier(enabled=True, detector=None)
    image = _make_image()
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.2, "normalized": True},
        {"label": 2, "center_x": 0.5, "center_y": 0.5, "score": 0.8, "normalized": True},
    ]
    result = verifier.verify(image, "click ok", annotations)
    assert [r["label"] for r in result] == [2, 1]


def test_verifier_reranks_by_second_inference():
    # First candidate verified as correct (model returns a point),
    # second candidate verified as incorrect (no points).
    detector = _FakeDetector([
        {"topk_points": [[(0.5, 0.5)]]},  # verifies candidate 1
        {"topk_points": []},  # verifies candidate 2 as incorrect
    ])
    verifier = UIVerifier(enabled=True, detector=detector, crop_size=32)
    image = _make_image((100, 100))
    annotations = [
        {"label": 1, "center_x": 0.3, "center_y": 0.3, "score": 0.5, "normalized": True},
        {"label": 2, "center_x": 0.7, "center_y": 0.7, "score": 0.9, "normalized": True},
    ]
    result = verifier.verify(image, "click ok", annotations)
    # Candidate 1 should win despite lower original score because verification succeeded.
    assert result[0]["label"] == 1
    assert result[0]["verify_score"] > result[1]["verify_score"]


def test_verifier_empty_annotations():
    verifier = UIVerifier(enabled=True)
    assert verifier.verify(_make_image(), "click ok", []) == []


def test_classify_pass():
    assert UIVerifier.classify(0.9) == VerifierVerdict.PASS
    assert UIVerifier.classify(PASS_THRESHOLD) == VerifierVerdict.PASS
    assert UIVerifier.classify(0.55) == VerifierVerdict.PASS


def test_classify_reject():
    assert UIVerifier.classify(0.0) == VerifierVerdict.REJECT
    assert UIVerifier.classify(REJECT_THRESHOLD) == VerifierVerdict.REJECT
    assert UIVerifier.classify(0.1) == VerifierVerdict.REJECT


def test_classify_uncertain():
    assert UIVerifier.classify(0.4) == VerifierVerdict.UNCERTAIN
    assert UIVerifier.classify(0.26) == VerifierVerdict.UNCERTAIN
    assert UIVerifier.classify(0.54) == VerifierVerdict.UNCERTAIN


def test_verify_disabled_marks_all_pass():
    verifier = UIVerifier(enabled=False)
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.3},
        {"label": 2, "center_x": 0.1, "center_y": 0.9, "score": 0.1},
    ]
    result = verifier.verify(None, "click the button", annotations)
    assert all(a["verdict"] == VerifierVerdict.PASS for a in result)


def test_verify_no_detector_marks_all_uncertain():
    verifier = UIVerifier(detector=None, enabled=True)
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.8},
    ]
    result = verifier.verify(None, "click the button", annotations)
    assert all(a["verdict"] == VerifierVerdict.UNCERTAIN for a in result)


def test_verify_sort_order_pass_before_reject():
    """Even low-confidence pass candidates sort before high-confidence rejects."""
    # Pass a truthy detector to route into the scored path where _verify_one is called.
    verifier = UIVerifier(enabled=True, detector=True)
    verifier._verify_one = lambda img, instr, ann: 0.55 if ann["label"] == 1 else 0.1
    annotations = [
        {"label": 1, "center_x": 0.5, "center_y": 0.5, "score": 0.3},  # will be pass
        {"label": 2, "center_x": 0.1, "center_y": 0.9, "score": 0.9},  # will be reject
    ]
    result = verifier.verify(None, "click the button", annotations)
    assert result[0]["verdict"] == VerifierVerdict.PASS
    assert result[0]["label"] == 1
