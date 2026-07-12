"""Visualize SoM annotations on screenshots: YOLO boxes or point markers."""

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


def _text_size(draw: ImageDraw.ImageDraw, text: str, font: Any) -> tuple[int, int]:
    bbox = draw.textbbox((0, 0), text, font=font) if font else (0, 0, 8, 12)
    return bbox[2] - bbox[0], bbox[3] - bbox[1]


def visualize_som(
    image: Image.Image,
    annotations: list[dict[str, Any]],
    marker_radius: int = 12,
    font_size: int = 14,
) -> Image.Image:
    """Draw numbered markers on a screenshot for SoM annotations.

    Annotations with a ``bbox`` (``[x1, y1, x2, y2]``, normalized [0, 1] by
    default) are drawn as red rectangles with a numbered pill at the top-left
    corner — the YOLO icon-detection style. Annotations without a ``bbox``
    fall back to a filled circle marker at ``center_x``/``center_y`` (the
    PreviewPoints style).

    Args:
        image: PIL RGB image.
        annotations: List of dicts with keys ``center_x``, ``center_y``,
            ``label`` (optional), ``bbox`` (optional), and ``normalized``
            (optional bool). Coordinates are normalized [0, 1] by default.
        marker_radius: Radius of the marker circle (no-bbox annotations).
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
        text = str(ann.get("label", "?"))
        normalized = ann.get("normalized", True)
        tw, th = _text_size(draw, text, font)

        bbox = ann.get("bbox")
        if bbox is not None:
            x1, y1, x2, y2 = bbox
            if normalized:
                x1, x2 = x1 * width, x2 * width
                y1, y2 = y1 * height, y2 * height
            x1, y1, x2, y2 = (int(round(v)) for v in (x1, y1, x2, y2))
            draw.rectangle([x1, y1, x2, y2], outline=(255, 0, 0, 255), width=2)
            # Numbered pill at the top-left corner — above the box when there
            # is room, inside the top edge otherwise.
            pad = 2
            ty = y1 - th - 2 * pad if y1 - th - 2 * pad >= 0 else y1
            draw.rounded_rectangle(
                [x1 - pad, ty - pad, x1 + tw + pad, ty + th + pad],
                radius=3,
                fill=(255, 0, 0, 220),
            )
            draw.text((x1, ty), text, fill=(255, 255, 255, 255), font=font)
            continue

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

        # Label above the marker with a small background pill.
        tx = cx - tw / 2
        ty = cy - marker_radius - th - 4
        pad = 2
        draw.rounded_rectangle(
            [tx - pad, ty - pad, tx + tw + pad, ty + th + pad],
            radius=4,
            fill=(0, 0, 0, 180),
        )
        draw.text((tx, ty), text, fill=(255, 255, 255, 255), font=font)

    return Image.alpha_composite(annotated, overlay).convert("RGB")
