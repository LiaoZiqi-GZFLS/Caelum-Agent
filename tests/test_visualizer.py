"""Tests for ui_detector.visualizer.visualize_som (YOLO boxes + SoM markers)."""

from __future__ import annotations

from PIL import Image

from ui_detector.visualizer import visualize_som


def _red_pixels(img: Image.Image) -> int:
    return sum(
        1 for p in img.get_flattened_data() if p[0] > 200 and p[1] < 80 and p[2] < 80
    )


def test_visualize_som_draws_bbox_rectangle():
    """Annotations with a normalized bbox get a red rectangle (YOLO style)."""
    image = Image.new("RGB", (100, 100), "white")
    anns = [
        {
            "label": 1,
            "center_x": 0.2,
            "center_y": 0.2,
            "bbox": [0.1, 0.1, 0.5, 0.5],
            "score": 0.9,
        }
    ]
    result = visualize_som(image, anns)

    assert result.size == (100, 100)
    # A 40x40 rectangle outline (plus label pill) leaves many red pixels.
    assert _red_pixels(result) > 100


def test_visualize_som_without_bbox_keeps_circle_marker():
    """PreviewPoints-style annotations (no bbox) keep the circle marker."""
    image = Image.new("RGB", (100, 100), "white")
    anns = [{"label": 1, "center_x": 0.5, "center_y": 0.5}]
    result = visualize_som(image, anns)

    assert _red_pixels(result) > 10


def test_visualize_som_bbox_and_circle_differ():
    """The bbox branch must produce visibly different ink from the circle one."""
    image = Image.new("RGB", (100, 100), "white")
    box = visualize_som(
        image,
        [{"label": 1, "center_x": 0.5, "center_y": 0.5, "bbox": [0.2, 0.2, 0.8, 0.8]}],
    )
    dot = visualize_som(image, [{"label": 1, "center_x": 0.5, "center_y": 0.5}])
    assert _red_pixels(box) != _red_pixels(dot)


def test_visualize_som_pixel_bbox():
    """bbox coordinates also accept raw pixels via normalized=False."""
    image = Image.new("RGB", (100, 100), "white")
    anns = [
        {
            "label": 1,
            "center_x": 25,
            "center_y": 25,
            "bbox": [10, 10, 40, 40],
            "normalized": False,
        }
    ]
    result = visualize_som(image, anns)
    assert _red_pixels(result) > 50


def test_visualize_som_empty_annotations_returns_unmodified_copy():
    """No annotations -> no ink at all, but the image content is preserved."""
    image = Image.new("RGB", (100, 100), "white")
    result = visualize_som(image, [])

    assert result.size == image.size
    assert _red_pixels(result) == 0
    assert result.get_flattened_data() == image.get_flattened_data()
