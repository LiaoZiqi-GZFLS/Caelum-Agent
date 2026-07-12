"""Tests for agent.preview_points (candidate coordinate preview markers)."""

from __future__ import annotations

import pytest
from PIL import Image

from agent.preview_points import mark_points, validate_points


def test_validate_accepts_one_to_three_points():
    assert validate_points([[10, 20]]) == [(10.0, 20.0)]
    assert validate_points([[1, 2], [3, 4], [5.5, 6.5]]) == [
        (1.0, 2.0),
        (3.0, 4.0),
        (5.5, 6.5),
    ]


def test_validate_rejects_zero_points():
    with pytest.raises(ValueError):
        validate_points([])


def test_validate_rejects_four_points():
    with pytest.raises(ValueError):
        validate_points([[1, 2], [3, 4], [5, 6], [7, 8]])


@pytest.mark.parametrize("bad", ["x", [1], [1, 2, 3], [["a", "b"]]])
def test_validate_rejects_malformed_points(bad):
    with pytest.raises(ValueError):
        validate_points([bad])


def test_mark_points_draws_red_marker():
    image = Image.new("RGB", (200, 200), "white")

    marked = mark_points(image, [(100.0, 100.0)])

    r, g, b = marked.getpixel((100, 100))
    assert r > 200 and g < 150 and b < 150
    # Untouched corners stay white.
    assert marked.getpixel((10, 10)) == (255, 255, 255)


def test_mark_points_numbers_each_candidate():
    image = Image.new("RGB", (200, 200), "white")

    marked = mark_points(image, [(50.0, 50.0), (150.0, 150.0)])

    for xy in ((50, 50), (150, 150)):
        r, g, b = marked.getpixel(xy)
        assert r > 200 and g < 150 and b < 150
