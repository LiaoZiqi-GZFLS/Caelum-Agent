"""PreviewPoints: visual confirmation of raw click coordinates.

When every structured locator fails (no UIA labels, vision pointing missed),
the model's last resort is guessing pixel coordinates from the compressed
screenshot. Guessing blind is error-prone, so this module turns it into a
preview-adjust-confirm loop: the model submits 1-3 candidate coordinates (in
the compressed screenshot's coordinate space), we draw numbered markers on a
clean copy of that screenshot, and the annotated image goes back to the model.
It adjusts or confirms, then clicks via ``windows__Click(loc=[x, y])`` — the
orchestrator rescales to physical pixels at execution time.
"""

from __future__ import annotations

from typing import Any

from PIL import Image

MAX_POINTS = 3


def validate_points(points: Any) -> list[tuple[float, float]]:
    """Validate model-supplied candidate points.

    Accepts a list of 1..MAX_POINTS ``[x, y]`` pairs (ints or floats) and
    returns them as float tuples. Raises ValueError on anything else.
    """
    if not isinstance(points, list) or not 1 <= len(points) <= MAX_POINTS:
        raise ValueError(f"points must be a list of 1-{MAX_POINTS} [x, y] pairs")
    validated: list[tuple[float, float]] = []
    for p in points:
        if (
            not isinstance(p, (list, tuple))
            or len(p) != 2
            or not all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in p)
        ):
            raise ValueError(f"invalid point {p!r}: expected [x, y] numbers")
        validated.append((float(p[0]), float(p[1])))
    return validated


def mark_points(image: Image.Image, points: list[tuple[float, float]]) -> Image.Image:
    """Draw numbered red markers at each point (pixel coordinates).

    Reuses the SoM visualizer with ``normalized=False`` so the marker style
    (red disc + numbered pill) matches what the model already knows from
    DesktopInteract. Returns a new RGB image; the input is untouched.
    """
    from ui_detector.visualizer import visualize_som

    annotations = [
        {
            "label": i + 1,
            "center_x": x,
            "center_y": y,
            "normalized": False,
        }
        for i, (x, y) in enumerate(points)
    ]
    return visualize_som(image, annotations)
