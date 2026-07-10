"""Verifier for GUI-Actor-3B predictions.

The verifier re-runs the same model on each candidate region to produce a
verification score, then re-ranks candidates by that score. This mirrors the
GUI-Actor paper's practice of using the same backbone for detection and
verification.
"""

from __future__ import annotations

import enum
from typing import Any

from PIL import Image


class VerifierVerdict(enum.StrEnum):
    PASS = "pass"
    REJECT = "reject"
    UNCERTAIN = "uncertain"


# Thresholds for verdict classification.
# verify_score >= PASS_THRESHOLD → pass
# verify_score <= REJECT_THRESHOLD → reject
# otherwise → uncertain
PASS_THRESHOLD = 0.55
REJECT_THRESHOLD = 0.25


class UIVerifier:
    """Re-rank GUI-Actor candidates by running a verification prompt.

    Args:
        detector: A loaded :class:`~ui_detector.detector.UIDetector` instance.
        enabled: Whether verification is active.
        crop_size: Size of the square patch (in pixels) cropped around each
            candidate for the verification pass.
    """

    def __init__(
        self,
        detector: Any | None = None,
        enabled: bool = True,
        crop_size: int = 224,
    ) -> None:
        self.detector = detector
        self.enabled = enabled
        self.crop_size = crop_size

    @staticmethod
    def classify(verify_score: float, pass_threshold: float = PASS_THRESHOLD, reject_threshold: float = REJECT_THRESHOLD) -> VerifierVerdict:
        """Classify a verify_score into pass / reject / uncertain."""
        if verify_score >= pass_threshold:
            return VerifierVerdict.PASS
        if verify_score <= reject_threshold:
            return VerifierVerdict.REJECT
        return VerifierVerdict.UNCERTAIN

    def verify(
        self,
        image: Image.Image,
        instruction: str,
        annotations: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Return annotations with verify_score and verdict, sorted by score.

        Each returned annotation is augmented with:
          - verify_score: float — combined verification score [0, 1]
          - verdict: str — "pass", "reject", or "uncertain"

        Annotations with verdict "reject" are still returned (sorted last) so
        callers can inspect them or count blocked candidates.
        """
        if not self.enabled or not annotations:
            return [
                {**a, "verify_score": 1.0, "verdict": VerifierVerdict.PASS}
                for a in sorted(annotations, key=lambda a: a.get("score", 0.0), reverse=True)
            ]

        if self.detector is None:
            return [
                {**a, "verify_score": float(a.get("score", 0.0)), "verdict": VerifierVerdict.UNCERTAIN}
                for a in sorted(annotations, key=lambda a: a.get("score", 0.0), reverse=True)
            ]

        scored: list[dict[str, Any]] = []
        for ann in annotations:
            score = self._verify_one(image, instruction, ann)
            verdict = self.classify(score)
            scored.append({**ann, "verify_score": score, "verdict": verdict})

        # Sort: pass first (by verify_score desc), then uncertain, then reject.
        order = {VerifierVerdict.PASS: 0, VerifierVerdict.UNCERTAIN: 1, VerifierVerdict.REJECT: 2}
        scored.sort(key=lambda a: (order.get(a["verdict"], 2), -a["verify_score"]))
        return scored

    def _verify_one(
        self, image: Image.Image, instruction: str, ann: dict[str, Any]
    ) -> float:
        """Score a single candidate by asking the model to verify it.

        The score combines the original detector confidence with a verification
        signal derived from a second inference pass on the cropped region.
        """
        width, height = image.size
        normalized = ann.get("normalized", True)
        cx = ann["center_x"]
        cy = ann["center_y"]
        if normalized:
            cx *= width
            cy *= height

        crop = self._crop_around(image, int(cx), int(cy))
        verify_instruction = (
            f"Task: {instruction}\n"
            f"Verify whether the highlighted element is the correct target. "
            f"If yes, click it. If no, do nothing."
        )

        try:
            pred = self.detector.predict(crop, verify_instruction, topk=1)
        except Exception:
            # Verification failed; keep the original score.
            return float(ann.get("score", 0.0))

        points = pred.get("topk_points") or []
        if not points or not points[0]:
            # Model declined to click -> likely incorrect.
            verify_signal = 0.0
        else:
            # Model clicked inside the crop -> likely correct.
            verify_signal = 1.0

        # Weighted combination of original score and verification signal.
        original = float(ann.get("score", 0.0))
        return 0.4 * original + 0.6 * verify_signal

    def _crop_around(
        self, image: Image.Image, cx: int, cy: int
    ) -> Image.Image:
        """Crop a square patch centered on (cx, cy), clamped to image bounds."""
        half = self.crop_size // 2
        width, height = image.size
        x1 = max(0, cx - half)
        y1 = max(0, cy - half)
        x2 = min(width, cx + half)
        y2 = min(height, cy + half)
        return image.crop((x1, y1, x2, y2))
