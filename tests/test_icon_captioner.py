"""Tests for ui_detector.icon_captioner — Florence-2 captioning of YOLO icons.

All tests fake the transformers model/processor — no weights or GPU needed.
"""

from __future__ import annotations

import pytest
from PIL import Image

import ui_detector.icon_captioner as ic


class _Batch(dict):
    """Processor output: a tensor-dict stand-in with .to(device[, dtype])."""

    def __init__(self, n):
        super().__init__({"pixel_values": [0] * n, "input_ids": [0] * n})
        self.n = n
        self.to_args = None

    def to(self, *args, **kwargs):
        self.to_args = (args, kwargs)
        return self


class FakeProcessor:
    """Stand-in for AutoProcessor: records batches, returns canned decodes."""

    batches: list[int] = []
    decodes: list[str] = []
    last_path: str | None = None
    last_trust_remote_code: bool | None = None
    last_skip_special_tokens: bool | None = None

    @classmethod
    def from_pretrained(cls, path, trust_remote_code=False):
        cls.last_path = path
        cls.last_trust_remote_code = trust_remote_code
        return cls()

    def __call__(self, text, images, return_tensors, padding):
        FakeProcessor.batches.append(len(images))
        return _Batch(len(images))

    def batch_decode(self, ids, skip_special_tokens):
        FakeProcessor.last_skip_special_tokens = skip_special_tokens
        # One decoded string per row, drawn from the canned queue.
        out = FakeProcessor.decodes[: len(ids)]
        del FakeProcessor.decodes[: len(ids)]
        return out


class FakeFlorence:
    """Stand-in for AutoModelForCausalLM (Florence-2 via trust_remote_code)."""

    instances: list = []
    fail_on_cuda = False
    last_path: str | None = None
    last_trust_remote_code: bool | None = None

    def __init__(self):
        self.device = None
        self.generate_calls: list[dict] = []
        FakeFlorence.instances.append(self)

    @classmethod
    def from_pretrained(cls, path, torch_dtype=None, trust_remote_code=False):
        cls.last_path = path
        cls.last_trust_remote_code = trust_remote_code
        return cls()

    def to(self, device):
        self.device = device
        return self

    def generate(self, pixel_values, input_ids, max_new_tokens, num_beams, do_sample):
        self.generate_calls.append(
            {"device": self.device, "max_new_tokens": max_new_tokens, "n": len(pixel_values)}
        )
        if FakeFlorence.fail_on_cuda and str(self.device).startswith("cuda"):
            raise RuntimeError("CUDA device-side assert")
        return [[1, 2]] * len(pixel_values)  # one fake id-row per crop


@pytest.fixture
def fake_models(monkeypatch):
    FakeFlorence.instances = []
    FakeFlorence.fail_on_cuda = False
    FakeProcessor.batches = []
    FakeProcessor.decodes = []
    FakeProcessor.last_skip_special_tokens = None
    monkeypatch.setattr(ic, "_FLORENCE_CLS", FakeFlorence)
    monkeypatch.setattr(ic, "_PROCESSOR_CLS", FakeProcessor)
    return FakeFlorence


def _crops(n, size=(20, 20)):
    return [Image.new("RGB", size) for _ in range(n)]


def _marker(label, score, text=None, icon=True, bbox=(0.1, 0.1, 0.3, 0.3)):
    return {
        "label": label,
        "center_x": (bbox[0] + bbox[2]) / 2,
        "center_y": (bbox[1] + bbox[3]) / 2,
        "bbox": list(bbox),
        "score": score,
        "text": text,
        "icon": icon,
    }


# ---------------------------------------------------------------------------
# caption_crops: batched <CAPTION> generation
# ---------------------------------------------------------------------------

def test_caption_crops_returns_one_caption_per_crop_in_order(fake_models):
    FakeProcessor.decodes = ["magnifier", "close button", "home icon"]
    cap = ic.IconCaptioner("model-dir")

    captions = cap.caption_crops(_crops(3))

    assert captions == ["magnifier", "close button", "home icon"]


def test_caption_crops_batches_by_batch_size(fake_models):
    FakeProcessor.decodes = ["a", "b", "c", "d", "e"]
    cap = ic.IconCaptioner("model-dir", batch_size=2)

    cap.caption_crops(_crops(5))

    assert FakeProcessor.batches == [2, 2, 1]


def test_caption_crops_empty_returns_empty(fake_models):
    cap = ic.IconCaptioner("model-dir")
    assert cap.caption_crops([]) == []
    assert FakeFlorence.instances == []  # never loaded


def test_caption_crops_passes_max_new_tokens(fake_models):
    FakeProcessor.decodes = ["x"]
    cap = ic.IconCaptioner("model-dir", max_new_tokens=42)

    cap.caption_crops(_crops(1))

    assert FakeFlorence.instances[0].generate_calls[0]["max_new_tokens"] == 42


def test_caption_crops_strips_special_tokens(fake_models):
    """Generation pads short captions to max_new_tokens; the model-facing
    description must never see raw <pad>/</s> tokens."""
    FakeProcessor.decodes = [
        "speaker button in dark mode.<pad><pad>",
        "magnifier</s>",
    ]
    cap = ic.IconCaptioner("model-dir")

    captions = cap.caption_crops(_crops(2))

    assert captions == ["speaker button in dark mode.", "magnifier"]
    assert FakeProcessor.last_skip_special_tokens is True


def test_unanswerable_captions_are_dropped(fake_models):
    """Florence-2 answers "unanswerable" when it can't caption a crop; a bare
    `icon` marker is more honest than a misleading "unanswerable" label."""
    FakeProcessor.decodes = ["unanswerable", "Unanswerable<pad><pad>", "magnifier"]
    cap = ic.IconCaptioner("model-dir")

    captions = cap.caption_crops(_crops(3))

    assert captions == ["", "", "magnifier"]


# ---------------------------------------------------------------------------
# Lazy loading and device fallback
# ---------------------------------------------------------------------------

def test_model_loads_lazily_on_first_caption(fake_models):
    FakeProcessor.decodes = ["a", "b"]
    cap = ic.IconCaptioner("model-dir")
    assert FakeFlorence.instances == []

    cap.caption_crops(_crops(1))
    assert len(FakeFlorence.instances) == 1
    cap.caption_crops(_crops(1))  # reuses the loaded model
    assert len(FakeFlorence.instances) == 1


def test_cuda_failure_falls_back_to_cpu_once(fake_models):
    FakeFlorence.fail_on_cuda = True
    FakeProcessor.decodes = ["recovered"]
    cap = ic.IconCaptioner("model-dir", device="cuda:0")

    captions = cap.caption_crops(_crops(1))

    assert captions == ["recovered"]
    assert cap.device == "cpu"
    devices = [c["device"] for c in FakeFlorence.instances[0].generate_calls]
    assert devices == ["cuda:0", "cpu"]


def test_model_loads_with_trust_remote_code_and_processor_path(fake_models):
    """Florence-2 has no native transformers support: the model must load via
    trust_remote_code, and the processor comes from the processor repo (the
    OmniParser icon_caption checkpoint ships no processor files)."""
    FakeProcessor.decodes = ["x"]
    cap = ic.IconCaptioner("model-dir", processor_path="proc-repo")

    cap.caption_crops(_crops(1))

    assert FakeFlorence.last_path == "model-dir"
    assert FakeFlorence.last_trust_remote_code is True
    assert FakeProcessor.last_path == "proc-repo"
    assert FakeProcessor.last_trust_remote_code is True


def test_default_processor_path_is_florence2_base_ft(fake_models):
    FakeProcessor.decodes = ["x"]
    cap = ic.IconCaptioner("model-dir")

    cap.caption_crops(_crops(1))

    assert FakeProcessor.last_path == "microsoft/Florence-2-base-ft"


def test_shutdown_releases_model_and_next_caption_reloads(fake_models):
    FakeProcessor.decodes = ["a", "b"]
    cap = ic.IconCaptioner("model-dir")
    cap.caption_crops(_crops(1))
    assert len(FakeFlorence.instances) == 1

    cap.shutdown()
    assert cap._model is None

    cap.caption_crops(_crops(1))
    assert len(FakeFlorence.instances) == 2


# ---------------------------------------------------------------------------
# caption_markers: bare icon markers get captions as their text
# ---------------------------------------------------------------------------

def test_caption_markers_writes_captions_into_bare_icon_text(fake_models):
    FakeProcessor.decodes = ["magnifier", "trash can"]
    cap = ic.IconCaptioner("model-dir")
    markers = [
        _marker(1, 0.9, text="搜索", icon=True),   # merged: keeps OCR text
        _marker(2, 0.8),                            # bare icon -> captioned
        _marker(3, 0.7, text="OK", icon=False),     # OCR-only: untouched
        _marker(4, 0.6),                            # bare icon -> captioned
    ]

    count = cap.caption_markers(Image.new("RGB", (100, 100)), markers, max_icons=30)

    assert count == 2
    assert markers[0]["text"] == "搜索"     # untouched
    assert markers[1]["text"] == "magnifier"
    assert markers[2]["text"] == "OK"       # untouched
    assert markers[3]["text"] == "trash can"


def test_caption_markers_caps_at_max_icons_highest_score_first(fake_models):
    FakeProcessor.decodes = ["cap-a", "cap-b"]
    cap = ic.IconCaptioner("model-dir")
    markers = [_marker(i, score=0.9 - i * 0.1) for i in range(1, 6)]

    count = cap.caption_markers(Image.new("RGB", (100, 100)), markers, max_icons=2)

    assert count == 2
    assert markers[0]["text"] == "cap-a"   # score 0.8 -> first
    assert markers[1]["text"] == "cap-b"   # score 0.7 -> second
    assert markers[2]["text"] is None


def test_caption_markers_crops_bbox_pixels(fake_models, monkeypatch):
    FakeProcessor.decodes = ["x"]
    seen_sizes: list[tuple[int, int]] = []
    real_crop = Image.Image.crop

    def spy_crop(self, box):
        seen_sizes.append((box[2] - box[0], box[3] - box[1]))
        return real_crop(self, box)

    monkeypatch.setattr(Image.Image, "crop", spy_crop)
    cap = ic.IconCaptioner("model-dir")
    markers = [_marker(1, 0.9, bbox=(0.1, 0.1, 0.3, 0.5))]

    cap.caption_markers(Image.new("RGB", (200, 100)), markers, max_icons=30)

    # 0.1*200=20 .. 0.3*200=60 wide; 0.1*100=10 .. 0.5*100=50 tall.
    assert seen_sizes == [(40, 40)]


def test_caption_markers_no_bare_icons_is_noop(fake_models):
    cap = ic.IconCaptioner("model-dir")
    markers = [_marker(1, 0.9, text="已有", icon=True)]

    count = cap.caption_markers(Image.new("RGB", (100, 100)), markers, max_icons=30)

    assert count == 0
    assert FakeFlorence.instances == []  # model never loaded
