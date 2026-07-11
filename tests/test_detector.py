"""Coverage for ui_detector.detector (GUI-Actor-3B wrapper)."""

from __future__ import annotations

import logging
import threading
from typing import Any

import pytest
import torch
from PIL import Image

import ui_detector.detector as detmod
from agent.config import UIDetectorConfig
from ui_detector.detector import UIDetector


def _cfg(**kw: Any) -> UIDetectorConfig:
    base: dict[str, Any] = {"device": "cpu", "dtype": "float32"}
    base.update(kw)
    return UIDetectorConfig(**base)


# ---------------------------------------------------------------------------
# __init__ / load
# ---------------------------------------------------------------------------

def test_init_creates_executor_and_verifier():
    d = UIDetector(_cfg(verifier={"enabled": False, "crop_size": 128}))
    try:
        assert d._executor._max_workers == 2
        assert d.verifier.enabled is False
        assert d.verifier.crop_size == 128
        assert d.model is None and d.processor is None and d.tokenizer is None
    finally:
        d.shutdown()


def test_load_cpu_path_records_kwargs(monkeypatch):
    calls: list[dict[str, Any]] = []

    class FakeModel:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            calls.append(kw)
            return "MODEL"

    class FakeTok:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            return "TOK"

    class FakeProc:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            return "PROC"

    monkeypatch.setattr(detmod, "Qwen2_5_VLForConditionalGenerationWithPointer", FakeModel)
    monkeypatch.setattr(detmod, "AutoTokenizer", FakeTok)
    monkeypatch.setattr(detmod, "AutoProcessor", FakeProc)

    d = UIDetector(_cfg(device="cpu", dtype="float32", attn_implementation="sdpa"))
    try:
        d.load()
        assert d.model == "MODEL" and d.tokenizer == "TOK" and d.processor == "PROC"
        assert d.verifier.detector is d
        assert calls[0]["device_map"] == "cpu"
        assert calls[0]["torch_dtype"] is torch.float32
        assert calls[0]["attn_implementation"] == "sdpa"
        assert calls[0]["trust_remote_code"] is True
    finally:
        d.shutdown()


def test_load_cuda_falls_back_when_unavailable(monkeypatch):
    calls: list[dict[str, Any]] = []

    class FakeModel:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            calls.append(kw)
            return "MODEL"

    monkeypatch.setattr(detmod, "Qwen2_5_VLForConditionalGenerationWithPointer", FakeModel)
    monkeypatch.setattr(detmod, "AutoTokenizer", type("T", (), {"from_pretrained": classmethod(lambda c, *a, **k: "TOK")}))
    monkeypatch.setattr(detmod, "AutoProcessor", type("P", (), {"from_pretrained": classmethod(lambda c, *a, **k: "PROC")}))
    monkeypatch.setattr(detmod.torch.cuda, "is_available", lambda: False)

    d = UIDetector(_cfg(device="cuda:0", dtype="bfloat16"))
    try:
        with pytest.warns(UserWarning, match="CUDA not available"):
            d.load()
        assert calls[0]["device_map"] == "cpu"
        assert calls[0]["torch_dtype"] is torch.float32  # forced on CPU fallback
    finally:
        d.shutdown()


def _stub_model_and_tokenizer(monkeypatch) -> None:
    monkeypatch.setattr(
        detmod,
        "Qwen2_5_VLForConditionalGenerationWithPointer",
        type("M", (), {"from_pretrained": classmethod(lambda c, *a, **k: "MODEL")}),
    )
    monkeypatch.setattr(
        detmod,
        "AutoTokenizer",
        type("T", (), {"from_pretrained": classmethod(lambda c, *a, **k: "TOK")}),
    )


def test_load_requests_fast_image_processor(monkeypatch):
    proc_calls: list[dict[str, Any]] = []

    class FakeProc:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            proc_calls.append(kw)
            return "PROC"

    _stub_model_and_tokenizer(monkeypatch)
    monkeypatch.setattr(detmod, "AutoProcessor", FakeProc)

    d = UIDetector(_cfg())
    try:
        d.load()
        assert d.processor == "PROC"
        assert proc_calls[0].get("use_fast") is True
    finally:
        d.shutdown()


def test_load_falls_back_to_slow_processor_when_fast_fails(monkeypatch, caplog):
    proc_calls: list[dict[str, Any]] = []

    class FakeProc:
        @classmethod
        def from_pretrained(cls, *a, **kw):
            proc_calls.append(kw)
            if kw.get("use_fast"):
                raise RuntimeError("fast image processor exploded")
            return "PROC"

    _stub_model_and_tokenizer(monkeypatch)
    monkeypatch.setattr(detmod, "AutoProcessor", FakeProc)

    d = UIDetector(_cfg())
    try:
        with caplog.at_level(logging.WARNING, logger="caelum.ui_detector"):
            d.load()
        assert d.processor == "PROC"
        assert len(proc_calls) == 2
        assert proc_calls[0].get("use_fast") is True
        assert "use_fast" not in proc_calls[1]
        assert "slow" in caplog.text.lower()
    finally:
        d.shutdown()


# ---------------------------------------------------------------------------
# ensure_loaded
# ---------------------------------------------------------------------------

def test_ensure_loaded_idempotent_and_thread_safe(monkeypatch):
    d = UIDetector(_cfg())
    calls = {"n": 0}
    lock = threading.Lock()

    def fake_load():
        with lock:
            calls["n"] += 1
        d.model = "M"
        d.processor = "P"
        d.tokenizer = "T"

    monkeypatch.setattr(d, "load", fake_load)
    try:
        threads = [threading.Thread(target=d.ensure_loaded) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        assert calls["n"] == 1
        d.ensure_loaded()  # second call is a no-op once model is set
        assert calls["n"] == 1
    finally:
        d.shutdown()


# ---------------------------------------------------------------------------
# predict / predict_async
# ---------------------------------------------------------------------------

def test_predict_calls_inference_with_default_topk(monkeypatch):
    d = UIDetector(_cfg(topk=4))
    d.model, d.processor, d.tokenizer = "M", "P", "T"
    monkeypatch.setattr(d, "ensure_loaded", lambda: None)

    captured: dict[str, Any] = {}

    def fake_inference(conversation, *, model, tokenizer, data_processor, topk):
        captured.update(model=model, tok=tokenizer, proc=data_processor, topk=topk)
        return {"topk_points": [], "topk_values": []}

    monkeypatch.setattr(detmod, "inference", fake_inference)
    try:
        out = d.predict(Image.new("RGB", (10, 10)), "click ok")
        assert out == {"topk_points": [], "topk_values": []}
        assert captured["topk"] == 4
        assert captured["model"] == "M"
    finally:
        d.shutdown()


def test_predict_respects_explicit_topk(monkeypatch):
    d = UIDetector(_cfg(topk=4))
    d.model, d.processor, d.tokenizer = "M", "P", "T"
    monkeypatch.setattr(d, "ensure_loaded", lambda: None)
    captured: dict[str, Any] = {}
    monkeypatch.setattr(
        detmod,
        "inference",
        lambda conv, *, model, tokenizer, data_processor, topk: captured.setdefault("topk", topk) or {},
    )
    try:
        d.predict(Image.new("RGB", (5, 5)), "x", topk=7)
        assert captured["topk"] == 7
    finally:
        d.shutdown()


@pytest.mark.asyncio
async def test_predict_async_runs_in_executor(monkeypatch):
    d = UIDetector(_cfg())
    monkeypatch.setattr(d, "predict", lambda image, instruction, topk=None: {"ok": 1})
    try:
        out = await d.predict_async(Image.new("RGB", (5, 5)), "x")
        assert out == {"ok": 1}
    finally:
        d.shutdown()


# ---------------------------------------------------------------------------
# annotate / visualize
# ---------------------------------------------------------------------------

class _FakeVerifier:
    def __init__(self, verdicts: list[str]) -> None:
        self._verdicts = verdicts

    def verify(self, image, instruction, annotations):
        for a, v in zip(annotations, self._verdicts):
            a["verdict"] = v
        return annotations


@pytest.mark.asyncio
async def test_annotate_parses_flat_points_and_filters_rejects(monkeypatch):
    d = UIDetector(_cfg())
    pred = {
        "topk_points": [(0.5, 0.5), (0.2, 0.8), ()],
        "topk_values": [0.9, 0.7, 0.1],
    }

    async def fake_predict(image, instruction):
        return pred

    monkeypatch.setattr(d, "predict_async", fake_predict)
    d.verifier = _FakeVerifier(["accept", "reject"])  # type: ignore[assignment]
    try:
        passed, blocked = await d.annotate(Image.new("RGB", (10, 10)), "go")
        assert blocked == 1
        assert len(passed) == 1
        assert passed[0]["center_x"] == 0.5 and passed[0]["center_y"] == 0.5
        assert passed[0]["normalized"] is True
        assert passed[0]["label"] == 1
    finally:
        d.shutdown()


@pytest.mark.asyncio
async def test_annotate_parses_legacy_nested_points(monkeypatch):
    d = UIDetector(_cfg())
    pred = {
        "topk_points": [[(0.1, 0.2), (0.3, 0.4)], [(0.5, 0.5)]],
        "topk_values": [0.8, 0.6],
    }

    async def fake_predict(image, instruction):
        return pred

    monkeypatch.setattr(d, "predict_async", fake_predict)
    d.verifier = _FakeVerifier(["accept", "accept"])  # type: ignore[assignment]
    try:
        passed, blocked = await d.annotate(Image.new("RGB", (10, 10)), "go")
        assert blocked == 0 and len(passed) == 2
        assert passed[0]["center_x"] == pytest.approx(0.2)
        assert passed[0]["center_y"] == pytest.approx(0.3)
        assert passed[1]["center_x"] == 0.5 and passed[1]["center_y"] == 0.5
    finally:
        d.shutdown()


@pytest.mark.asyncio
async def test_visualize_returns_annotated_image(monkeypatch):
    d = UIDetector(_cfg())
    sentinel = Image.new("RGB", (7, 7), "green")

    async def fake_annotate(image, instruction):
        return ([{"center_x": 0.5, "center_y": 0.5, "score": 1.0}], 0)

    monkeypatch.setattr(d, "annotate", fake_annotate)
    monkeypatch.setattr(detmod, "visualize_som", lambda img, ann, r, f: sentinel)
    try:
        out = await d.visualize(Image.new("RGB", (10, 10)), "go")
        assert out is sentinel
    finally:
        d.shutdown()


def test_shutdown_marks_executor():
    d = UIDetector(_cfg())
    d.shutdown()
    assert d._executor._shutdown is True
