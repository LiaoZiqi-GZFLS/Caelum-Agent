"""Visualize GUI-Actor SoM annotations on screenshots."""

from __future__ import annotations

from typing import Any

from PIL import Image, ImageDraw, ImageFont


def _resolve_font(size: int) -> ImageFont.ImageFont | None:
    """Return a TrueType font if available, otherwise fall back to default."""
    try:
        return ImageFont.truetype("arial.ttf", size)
    except Exception:
        try:
            return ImageFont.truetype("DejaVuSans.ttf", size)
        except Exception:
            return None


def visualize_som(
    image: Image.Image,
    annotations: list[dict[str, Any]],
    marker_radius: int = 12,
    font_size: int = 14,
) -> Image.Image:
    """Draw numbered markers on a screenshot for SoM annotations.

    Args:
        image: PIL RGB image.
        annotations: List of dicts with keys ``center_x``, ``center_y``,
            ``label`` (optional), and ``normalized`` (optional bool).
            Coordinates are normalized [0, 1] by default.
        marker_radius: Radius of the marker circle.
        font_size: Size of the label text.

    Returns:
        A new PIL RGB image with markers drawn.
    """
    annotated = image.convert("RGBA")
    overlay = Image.new("RGBA", annotated.size, (255, 255, 255, 0))
    draw = ImageDraw.Draw(overlay)
    font = _resolve_font(font_size)

    width, height = annotated.size
    for ann in annotations:
        label = ann.get("label", "?")
        text = str(label)
        normalized = ann.get("normalized", True)
        cx = ann["center_x"]
        cy = ann["center_y"]
        if normalized:
            cx *= width
            cy *= height
        cx = int(round(cx))
        cy = int(round(cy))

        # Marker circle.
        draw.ellipse(
            [
                (cx - marker_radius, cy - marker_radius),
                (cx + marker_radius, cy + marker_radius),
            ],
            fill=(255, 0, 0, 180),
            outline=(255, 255, 255, 220),
            width=2,
        )

        # Label inside/above the marker.
        bbox = draw.textbbox((0, 0), text, font=font) if font else (0, 0, 8, 12)
        tw = bbox[2] - bbox[0]
        th = bbox[3] - bbox[1]
        tx = cx - tw / 2
        ty = cy - marker_radius - th - 4
        # Small background pill for readability.
        pad = 2
        draw.rounded_rectangle(
            [tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
            radius=4,
            fill=(0, 0, 0, 180),
        )
        draw.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(annotated, overlay).convert("RGB")
