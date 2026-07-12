"""Unit tests for ui_detector.yolo_detector.YoloDetector and agent.config.YoloConfig.

All tests mock ultralytics — no model weights or GPU needed.
"""

from __future__ import annotations

import pytest
from PIL import Image

import ui_detector.yolo_detector as yd
from agent.config import Config, YoloConfig


class _T(list):
    """List with a torch-like .tolist() so fakes mimic ultralytics tensors."""

    def tolist(self):
        return list(self)


class _FakeBox:
    def __init__(self, xyxy, conf):
        self.xyxy = [_T(xyxy)]
        self.conf = _T([conf])


class _FakeResult:
    def __init__(self, boxes):
        self.boxes = boxes


class FakeYOLO:
    """Drop-in for ultralytics.YOLO; class attrs steer each test."""

    instances: list = []
    fail_on_cuda = False
    boxes: list = []  # list of ([x1, y1, x2, y2], conf)

    def __init__(self, path):
        self.path = path
        self.predict_calls = []
        FakeYOLO.instances.append(self)

    def predict(self, image, imgsz, conf, device, verbose):
        self.predict_calls.append(
            {"imgsz": imgsz, "conf": conf, "device": device, "verbose": verbose}
        )
        if FakeYOLO.fail_on_cuda and str(device).startswith("cuda"):
            raise RuntimeError("CUDA device-side assert")
        return [_FakeResult([_FakeBox(*b) for b in FakeYOLO.boxes])]


@pytest.fixture
def fake_yolo(monkeypatch):
    FakeYOLO.instances = []
    FakeYOLO.fail_on_cuda = False
    FakeYOLO.boxes = []
    # Pre-seed the lazy class holder so no real ultralytics import happens.
    monkeypatch.setattr(yd, "_YOLO", FakeYOLO)
    return FakeYOLO


def _image(w=1000, h=500):
    return Image.new("RGB", (w, h))


# ---------------------------------------------------------------------------
# detect() contract
# ---------------------------------------------------------------------------

def test_detect_returns_normalized_annotations(fake_yolo):
    FakeYOLO.boxes = [
        ([100, 50, 300, 150], 0.9),
        ([400, 200, 600, 400], 0.7),
    ]
    det = yd.YoloDetector("model.pt")
    anns = det.detect(_image(1000, 500))

    assert len(anns) == 2
    first = anns[0]
    assert first["label"] == 1
    assert first["center_x"] == pytest.approx(0.2)  # (100+300)/2 / 1000
    assert first["center_y"] == pytest.approx(0.2)  # (50+150)/2 / 500
    assert first["bbox"] == pytest.approx([0.1, 0.1, 0.3, 0.3])
    assert first["score"] == pytest.approx(0.9)
    assert anns[1]["label"] == 2


def test_detect_sorts_by_score_descending(fake_yolo):
    FakeYOLO.boxes = [
        ([400, 200, 600, 400], 0.4),
        ([100, 50, 300, 150], 0.95),
    ]
    det = yd.YoloDetector("model.pt")
    anns = det.detect(_image())
    assert anns[0]["score"] == pytest.approx(0.95)
    assert anns[0]["label"] == 1
    assert anns[1]["score"] == pytest.approx(0.4)


def test_detect_empty_returns_empty_list(fake_yolo):
    det = yd.YoloDetector("model.pt")
    assert det.detect(_image()) == []


def test_detect_passes_predict_parameters(fake_yolo):
    FakeYOLO.boxes = [([0, 0, 10, 10], 0.9)]
    det = yd.YoloDetector("model.pt", device="cuda:0", conf=0.3, imgsz=640)
    det.detect(_image())
    call = FakeYOLO.instances[0].predict_calls[0]
    assert call["imgsz"] == 640
    assert call["conf"] == 0.3
    assert call["device"] == "cuda:0"
    assert call["verbose"] is False


# ---------------------------------------------------------------------------
# Lazy loading and device fallback
# ---------------------------------------------------------------------------

def test_model_loads_lazily_on_first_detect(fake_yolo):
    det = yd.YoloDetector("model.pt")
    assert FakeYOLO.instances == []
    det.detect(_image())
    assert len(FakeYOLO.instances) == 1
    det.detect(_image())  # second call reuses the loaded model
    assert len(FakeYOLO.instances) == 1


def test_cuda_failure_falls_back_to_cpu_once(fake_yolo):
    FakeYOLO.fail_on_cuda = True
    FakeYOLO.boxes = [([0, 0, 10, 10], 0.9)]
    det = yd.YoloDetector("model.pt", device="cuda:0")
    anns = det.detect(_image())

    assert len(anns) == 1
    assert det.device == "cpu"
    calls = FakeYOLO.instances[0].predict_calls
    assert [c["device"] for c in calls] == ["cuda:0", "cpu"]
    # Subsequent detections stay on cpu (no repeated cuda attempts).
    det.detect(_image())
    assert FakeYOLO.instances[0].predict_calls[-1]["device"] == "cpu"


def test_cpu_failure_propagates(monkeypatch):
    class _Boom(FakeYOLO):
        def predict(self, *a, **k):
            raise RuntimeError("cpu boom")

    monkeypatch.setattr(yd, "_YOLO", _Boom)
    det = yd.YoloDetector("model.pt", device="cpu")
    with pytest.raises(RuntimeError, match="cpu boom"):
        det.detect(_image())


# ---------------------------------------------------------------------------
# YoloConfig
# ---------------------------------------------------------------------------

def test_yolo_config_defaults():
    cfg = YoloConfig()
    assert cfg.enabled is True
    assert cfg.model_path == "./models/omniparser/icon_detect/model.pt"
    assert cfg.device == "cuda:0"
    assert cfg.conf == pytest.approx(0.25)
    assert cfg.imgsz == 1280
    assert cfg.auto_compensate is True


def test_config_exposes_yolo_section():
    cfg = Config.model_validate({"llm": {"api_key": "test"}})
    assert isinstance(cfg.yolo, YoloConfig)
    assert cfg.yolo.enabled is True
