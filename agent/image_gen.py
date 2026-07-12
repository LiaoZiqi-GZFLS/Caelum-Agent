"""GenerateImage: an SVG-generating subagent with visual self-review.

The main agent stays clean: this tool spins up a separate message context that
(1) asks the LLM for SVG code matching the requirement, (2) rasterizes it to
PNG with CairoSVG, (3) uploads the PNG and asks the LLM to visually review it
against the requirement (JSON verdict), and (4) loops with the reviewer's
feedback — at most ``max_rounds`` times. The final PNG path is returned either
way; a failed review budget is reported, not hidden.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
from pathlib import Path
from typing import Any

logger = logging.getLogger("caelum.image_gen")

MAX_ROUNDS = 5

_SVG_RE = re.compile(r"<svg[\s\S]*?</svg>", re.IGNORECASE)

_SYSTEM_PROMPT = (
    "You are an SVG illustration generator. Output ONLY a single self-contained "
    "<svg>...</svg> document (optionally fenced in ```svg```). Rules: include "
    "xmlns=\"http://www.w3.org/2000/svg\", an explicit width/height or viewBox; "
    "no external references (no href/url() to remote resources, no fonts that "
    "require network); no <script>. Use simple shapes, paths, gradients and "
    "text to faithfully realize the user's requirement."
)

_REVIEW_PROMPT = (
    "You are reviewing a generated image against the requirement below. "
    "Answer in JSON: {{\"ok\": true/false, \"issues\": \"...\"}}. "
    "\"ok\" is true only if the image faithfully and completely matches the "
    "requirement (subject, layout, colors, text if any). Otherwise describe "
    "the concrete problems in \"issues\" so the generator can fix them.\n\n"
    "Requirement: {requirement}"
)

_RETRY_TEMPLATE = (
    "The rendered image was reviewed and rejected. Issues: {issues}\n"
    "Output the FULL corrected SVG only."
)


def extract_svg(text: str) -> str | None:
    """Pull the first <svg>...</svg> document out of an LLM response."""
    match = _SVG_RE.search(text or "")
    return match.group(0) if match else None


class ImageGenerator:
    """Generate an SVG, rasterize it, and visually verify it (max 5 rounds)."""

    def __init__(
        self,
        llm: Any,
        uploader: Any,
        out_dir: Path | str,
        max_rounds: int = MAX_ROUNDS,
    ) -> None:
        self.llm = llm
        self.uploader = uploader
        self.out_dir = Path(out_dir)
        self.max_rounds = max_rounds

    async def generate(self, requirement: str) -> dict[str, Any]:
        """Return {path, rounds, ok, issues} for the best attempt."""
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": requirement},
        ]
        last_path: Path | None = None
        last_issues = "no SVG produced"
        for round_no in range(1, self.max_rounds + 1):
            completion = await self.llm.chat(messages, tools=None)
            content = completion.choices[0].message.content or ""
            svg = extract_svg(content)
            if svg is None:
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": _RETRY_TEMPLATE.format(
                        issues="your reply contained no <svg> markup"
                    ),
                })
                last_issues = "no SVG markup in reply"
                continue
            try:
                png_path = await asyncio.to_thread(self._render, svg)
            except Exception as exc:
                logger.warning("GenerateImage round %d render failed: %s", round_no, exc)
                messages.append({"role": "assistant", "content": content})
                messages.append({
                    "role": "user",
                    "content": _RETRY_TEMPLATE.format(
                        issues=f"the SVG failed to render ({exc})"
                    ),
                })
                last_issues = f"render failed: {exc}"
                continue
            last_path = png_path
            _, url = await self.uploader.upload(png_path)
            ok, issues = await self._review(requirement, url)
            if ok:
                return {"path": png_path, "rounds": round_no, "ok": True, "issues": ""}
            last_issues = issues
            messages.append({"role": "assistant", "content": content})
            messages.append({
                "role": "user",
                "content": _RETRY_TEMPLATE.format(issues=issues or "unspecified"),
            })
        return {
            "path": last_path,
            "rounds": self.max_rounds,
            "ok": False,
            "issues": last_issues,
        }

    def _render(self, svg: str) -> Path:
        """Rasterize SVG to PNG under out_dir, named by content hash."""
        import cairosvg  # imported lazily: requires the native cairo library

        self.out_dir.mkdir(parents=True, exist_ok=True)
        digest = hashlib.sha256(svg.encode("utf-8")).hexdigest()[:8]
        path = self.out_dir / f"gen-{digest}.png"
        cairosvg.svg2png(bytestring=svg.encode("utf-8"), write_to=str(path))
        return path

    async def _review(self, requirement: str, url: str) -> tuple[bool, str]:
        """Ask the LLM to visually compare the rendered image to the requirement."""
        completion = await self.llm.chat(
            [{
                "role": "user",
                "content": [
                    {"type": "image_url", "image_url": {"url": url}},
                    {"type": "text", "text": _REVIEW_PROMPT.format(requirement=requirement)},
                ],
            }],
            tools=None,
            response_format={"type": "json_object"},
        )
        content = completion.choices[0].message.content or ""
        try:
            verdict = json.loads(content)
            return bool(verdict.get("ok")), str(verdict.get("issues", ""))
        except (json.JSONDecodeError, AttributeError):
            # Unparseable verdict: treat as rejection with the raw text as feedback.
            return False, content[:500]


def make_generate_image_handler(generator: ImageGenerator):
    """Build the async GenerateImage tool handler."""

    async def generate_image(requirement: str) -> str:
        result = await generator.generate(requirement)
        if result["path"] is None:
            return (
                f"[error] image generation produced no renderable SVG after "
                f"{result['rounds']} rounds (last issue: {result['issues']})"
            )
        status = (
            f"verified OK after {result['rounds']} round(s)"
            if result["ok"]
            else f"NOT verified after {result['rounds']} rounds "
                 f"(last issue: {result['issues']})"
        )
        return f"[generate_image] {status}: {result['path']}"

    return generate_image


GENERATE_IMAGE_SCHEMA = {
    "type": "object",
    "properties": {
        "requirement": {
            "type": "string",
            "description": (
                "What the image should depict, as detailed as needed: subject, "
                "style, layout, colors, text content."
            ),
        },
    },
    "required": ["requirement"],
}


def register_generate_image(
    llm: Any, config: Any, cache_dir: Path | str, uploader: Any | None
) -> ImageGenerator | None:
    """Register the GenerateImage tool; needs a MediaUploader for review."""
    if uploader is None or not getattr(config, "enable_media_upload", True):
        return None
    generator = ImageGenerator(
        llm, uploader, Path(cache_dir) / "generated"
    )
    llm.register_local_function(
        "GenerateImage",
        make_generate_image_handler(generator),
        schema=GENERATE_IMAGE_SCHEMA,
        description=(
            "Generate an image from a text requirement. A subagent writes SVG, "
            "renders it to PNG, and visually self-reviews it against your "
            "requirement (up to 5 revision rounds). Returns the local PNG "
            "path. Use for illustrations, icons, diagrams, and mockups."
        ),
    )
    return generator
