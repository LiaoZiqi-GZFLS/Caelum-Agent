"""Tool handler implementations extracted from the orchestrator.

Each tool has an ``_impl`` handler (registered via ``register_local_function``)
and a ``_register_*`` method that wires it into the LLM client. The handlers
live in a mixin class so ``self.`` references (mcp, perception, config, etc.)
resolve against the orchestrator instance at runtime.
"""

from __future__ import annotations

import asyncio
import math
from pathlib import Path
from typing import Any

from agent.perception import ZOOM_REGION_SIZES
from agent.tools import (
    COMPLETE_TASK_SCHEMA,
    DESKTOP_INTERACT_SCHEMA,
    NEARBY_LABELS_SCHEMA,
    PREVIEW_POINTS_SCHEMA,
    UPGRADE_VISION_SCHEMA,
    WAIT_SCHEMA,
    ZOOM_REGION_SCHEMA,
)


class ToolHandlersMixin:
    """Mixin providing tool handler methods for AgentOrchestrator.

    All ``self.`` attribute accesses (mcp, perception, config, llm,
    _last_perception, etc.) are resolved on the orchestrator instance
    at runtime — this class never stands alone.
    """

    # ------------------------------------------------------------------
    # DesktopInteract
    # ------------------------------------------------------------------

    async def _desktop_interact_impl(
        self,
        label: int | None = None,
        action: str = "click",
        text: str | None = None,
    ) -> str:
        """Execute an action on a YOLO-annotated element by its marker label.

        Labels come from the latest perception's SoM annotations — the
        numbered red boxes on the annotated screenshot the model just saw.
        No detection pass runs here: the label space is exactly what the
        last perception produced (YOLO runs automatically on UIA-less
        frames, and ZoomRegion produces its own annotated view).
        """
        perception = getattr(self, "_last_perception", None)
        if perception is None:
            return "[error] No perception data available. Run perception first."
        anns = perception.som_annotations
        if not anns:
            return (
                "[error] The latest perception has no YOLO annotations (the "
                "UIA tree was usable, so no detection ran). Use a "
                "windows__Snapshot label instead, or PreviewPoints for raw "
                "coordinates."
            )
        if label is None:
            available = [a.get("label") for a in anns]
            return (
                "[error] DesktopInteract requires label=<marker number> from "
                f"the annotated screenshot. Available labels: {available}"
            )
        match = next((a for a in anns if a.get("label") == label), None)
        if match is None:
            available = [a.get("label") for a in anns]
            return f"[error] SoM label {label} not found. Available labels: {available}"

        # Convert normalized [0,1] to screen pixel coordinates, honoring the
        # covered area's native origin (full-screen views have origin (0, 0);
        # ZoomRegion views carry the crop's top-left corner).
        sw = perception.screen_width or 1920
        sh = perception.screen_height or 1080
        ox = getattr(perception, "image_origin_x", 0) or 0
        oy = getattr(perception, "image_origin_y", 0) or 0
        screen_x = int(round(ox + match.get("center_x", 0) * sw))
        screen_y = int(round(oy + match.get("center_y", 0) * sh))

        if action in ("click", "double_click", "right_click"):
            mcp_action = "Click"
            mcp_args: dict[str, Any] = {"loc": [screen_x, screen_y]}
            if action == "double_click":
                mcp_args["clicks"] = 2
            elif action == "right_click":
                mcp_args["button"] = "right"
        elif action == "type":
            if self._check_cancelled():
                return "[error] Task cancelled by kill switch."
            focus_result = await self.mcp.call("windows", "Click", {"loc": [screen_x, screen_y]})
            if not focus_result.success:
                return f"[error] Failed to focus element at ({screen_x}, {screen_y}): {focus_result.content}"
            if self._check_cancelled():
                return "[error] Task cancelled by kill switch."
            type_result = await self.mcp.call("windows", "Type", {"text": text or ""})
            if type_result.success:
                return f"OK: typed text at ({screen_x}, {screen_y}) — {type_result.content[:200]}"
            return f"[error] {type_result.content}"
        elif action in ("scroll_down", "scroll_up"):
            direction = "down" if action == "scroll_down" else "up"
            if self._check_cancelled():
                return "[error] Task cancelled by kill switch."
            scroll_result = await self.mcp.call("windows", "Scroll", {
                "loc": [screen_x, screen_y],
                "direction": direction,
            })
            if scroll_result.success:
                return f"OK: {action} at ({screen_x}, {screen_y}) — {scroll_result.content[:200]}"
            return f"[error] {scroll_result.content}"
        else:
            return f"[error] Unknown action: {action}"

        if self._check_cancelled():
            return "[error] Task cancelled by kill switch."
        result = await self.mcp.call("windows", mcp_action, mcp_args)
        if result.success:
            return f"OK: {action} at ({screen_x}, {screen_y}) — {result.content[:200]}"
        return f"[error] {result.content}"

    def _register_desktop_interact(self) -> None:
        """Register the DesktopInteract local function tool with the LLM."""
        self.llm.register_local_function(
            "DesktopInteract",
            self._desktop_interact_impl,
            schema=DESKTOP_INTERACT_SCHEMA,
            description=(
                "Interact with a UI element by its marker label: label=<number> "
                "from the annotated screenshot (numbered red boxes on detected "
                "icons AND recognized text; each perception lists every "
                "marker with its content — pick the label whose text/icon "
                "matches your target). Works on ANY app, including ones whose "
                "UIA tree is missing, empty, or inaccurate (Qt apps like "
                "WeChat/QQ, Electron apps, games, custom-drawn controls) — on "
                "those screens the annotated image is attached automatically. "
                "PREFER this over raw coordinates whenever a marker sits on "
                "your target, and use it when windows__Snapshot shows no "
                "usable element or label-based clicks land wrong. When no "
                "marker fits your target, use PreviewPoints for coordinate "
                "guesses. Actions: click (default), double_click, right_click, "
                "type (needs text=), scroll_down/up."
            ),
        )

    # ------------------------------------------------------------------
    # ZoomRegion
    # ------------------------------------------------------------------

    async def _zoom_region_impl(
        self,
        size: str,
        label: int | None = None,
        loc: list | None = None,
    ) -> str:
        """Handler for ZoomRegion: re-perceive a region at original resolution.

        Resolves the region center from a YOLO marker label or raw
        coordinates (current screenshot space) into native screen pixels,
        then asks perception for a full region view (OCR + YOLO boxes on a
        native crop). The region view replaces _last_perception — its origin
        offset makes DesktopInteract/PreviewPoints coordinates convert
        automatically — and is stashed so _think_and_act attaches its clean +
        annotated images right after this tool result.
        """
        perception = getattr(self, "_last_perception", None)
        if perception is None:
            return "[error] No perception data available. Run perception first."
        if (label is None) == (loc is None):
            return (
                "[error] ZoomRegion needs exactly one center: label=<marker "
                "number> or loc=[x, y] (current screenshot space)."
            )
        px = ZOOM_REGION_SIZES.get(size)
        if px is None:
            return (
                f"[error] Unknown size {size!r}; choose one of "
                f"{sorted(ZOOM_REGION_SIZES)}."
            )

        sw = perception.screen_width or 0
        sh = perception.screen_height or 0
        ox = getattr(perception, "image_origin_x", 0) or 0
        oy = getattr(perception, "image_origin_y", 0) or 0
        if label is not None:
            match = next(
                (a for a in perception.som_annotations if a.get("label") == label),
                None,
            )
            if match is None:
                available = [a.get("label") for a in perception.som_annotations]
                return (
                    f"[error] SoM label {label} not found. Available labels: "
                    f"{available}"
                )
            center_x = int(round(ox + match.get("center_x", 0) * sw))
            center_y = int(round(oy + match.get("center_y", 0) * sh))
        else:
            if not (isinstance(loc, (list, tuple)) and len(loc) == 2):
                return "[error] loc must be [x, y] in normalized [0,1] coordinates."
            if not (sw and sh):
                return (
                    "[error] Current perception lacks dimensions; cannot map "
                    "loc to the screen."
                )
            center_x = int(round(ox + float(loc[0]) * sw))
            center_y = int(round(oy + float(loc[1]) * sh))

        region = await self.perception.perceive_region(center_x, center_y, px)
        self._set_last_perception(region)
        self._pending_region = region
        return (
            f"[ok] Zoomed {size} region ({px}px) around screen ({center_x}, "
            f"{center_y}) attached below: clean image + YOLO-annotated copy. "
            "Labels and loc coordinates now refer to the region image in "
            "normalized [0,1] coordinates; DesktopInteract(label=N) and "
            "PreviewPoints convert to screen pixels automatically. The next "
            "perception round returns to the full screen."
        )

    def _register_zoom_region(self) -> None:
        """Register the ZoomRegion local function tool with the LLM."""
        self.llm.register_local_function(
            "ZoomRegion",
            self._zoom_region_impl,
            schema=ZOOM_REGION_SCHEMA,
            description=(
                "Zoom into a screen region at ORIGINAL resolution for a closer "
                "look: crops a square around label=<marker number> or "
                "loc=[x, y] (normalized [0,1]) and runs full "
                "perception on it (OCR + YOLO boxes), attaching clean + "
                "annotated images. size: small=480, medium=960, large=1680 "
                "native px — pick the smallest tier covering your target. Use "
                "when text is too small to read or no marker covers your "
                "target. After zooming, DesktopInteract(label=...) and loc "
                "coordinates refer to the region image in normalized [0,1] "
                "(conversion is automatic); the next perception round returns "
                "to the full screen."
            ),
        )

    # ------------------------------------------------------------------
    # NearbyLabels
    # ------------------------------------------------------------------

    async def _nearby_labels_impl(
        self,
        label: int | None = None,
        loc: list | None = None,
        k: int = 6,
    ) -> str:
        """Handler for NearbyLabels: list markers nearest to a query point.

        Pure geometry over the current perception's YOLO annotations — no new
        capture or detection. All coordinates and distances are normalized
        [0,1]; the model clicks the closest match with DesktopInteract(label=N)
        or refines a coordinate guess with PreviewPoints.
        """
        perception = getattr(self, "_last_perception", None)
        if perception is None:
            return "[error] No perception data available. Run perception first."
        anns = perception.som_annotations
        if not anns:
            return (
                "[error] The current view has no YOLO annotations. Use a "
                "windows__Snapshot label, ZoomRegion to re-perceive an area, "
                "or PreviewPoints for raw coordinates."
            )
        if (label is None) == (loc is None):
            return (
                "[error] NearbyLabels needs exactly one query point: "
                "label=<marker number> or loc=[x, y] (normalized [0,1])."
            )

        if label is not None:
            match = next(
                (a for a in anns if a.get("label") == label), None
            )
            if match is None:
                available = [a.get("label") for a in anns]
                return (
                    f"[error] SoM label {label} not found. Available labels: "
                    f"{available}"
                )
            qx = float(match.get("center_x", 0))
            qy = float(match.get("center_y", 0))
            pool = [a for a in anns if a is not match]
        else:
            if not (isinstance(loc, (list, tuple)) and len(loc) == 2):
                return "[error] loc must be [x, y] in normalized [0,1] coordinates."
            qx, qy = float(loc[0]), float(loc[1])
            pool = list(anns)

        try:
            k = max(1, int(k))
        except (TypeError, ValueError):
            k = 6

        scored = []
        for ann in pool:
            cx = float(ann.get("center_x", 0))
            cy = float(ann.get("center_y", 0))
            scored.append((math.hypot(cx - qx, cy - qy), ann, cx, cy))
        scored.sort(key=lambda t: t[0])
        scored = scored[:k]
        if not scored:
            return "[error] No other markers near the query point."

        lines = [
            f"  label {ann.get('label')} at ({cx:.4f}, {cy:.4f}) — "
            f"distance {dist:.4f}"
            for dist, ann, cx, cy in scored
        ]
        return (
            f"[nearby labels] closest to ({qx:.4f}, {qy:.4f}):\n"
            + "\n".join(lines)
            + "\nUse DesktopInteract(label=N) to click one, or PreviewPoints "
              "with adjusted coordinates."
        )

    def _register_nearby_labels(self) -> None:
        """Register the NearbyLabels local function tool with the LLM."""
        self.llm.register_local_function(
            "NearbyLabels",
            self._nearby_labels_impl,
            schema=NEARBY_LABELS_SCHEMA,
            description=(
                "List the YOLO markers nearest to a point: label=<marker "
                "number> or loc=[x, y] (normalized [0,1]), plus "
                "optional k (default 6). Returns marker labels, centers, and "
                "distances sorted nearest-first. Use when no marker covers "
                "your exact target: pick the closest with "
                "DesktopInteract(label=N), or use a neighbor's coordinates to "
                "refine a PreviewPoints guess."
            ),
        )

    # ------------------------------------------------------------------
    # UpgradeVision
    # ------------------------------------------------------------------

    async def _upgrade_vision_impl(self) -> str:
        """Handler for UpgradeVision: switch screenshots to the original image.

        Sets the perception override so every subsequent screenshot (main-loop
        and SoM passes) is the full native-resolution original, and flags
        _think_and_act to inject a fresh full-res perception right after this
        tool result so the model can continue without wasting a round.
        """
        self.perception.original_resolution = True
        self._upgrade_requested = True
        return (
            "[ok] Vision upgraded to the ORIGINAL (full native) resolution "
            "for the rest of this task. A fresh full-resolution screenshot "
            "follows."
        )

    def _register_upgrade_vision(self) -> None:
        """Register the UpgradeVision local function tool with the LLM."""
        self.llm.register_local_function(
            "UpgradeVision",
            self._upgrade_vision_impl,
            schema=UPGRADE_VISION_SCHEMA,
            description=(
                "Upgrade screenshots to the ORIGINAL (full native) resolution "
                "for the rest of this task. Use when you repeatedly cannot "
                "read small text or locate elements in the screenshot. A "
                "fresh full-resolution screenshot is attached right after the call."
            ),
        )

    # ------------------------------------------------------------------
    # PreviewPoints
    # ------------------------------------------------------------------

    async def _preview_points_impl(self, points: list) -> str:
        """Preview candidate click coordinates as numbered markers.

        Last-resort locator: when UIA labels and SoM markers both fail,
        the model guesses coordinates from the compressed screenshot. This
        draws the guesses on a clean copy and shows them back (via the
        _pending_preview follow-up appended by _think_and_act) so the model
        can adjust before committing to a real click through windows__Click.
        """
        from PIL import Image

        from agent.preview_points import mark_points, validate_points

        try:
            pts = validate_points(points)
        except ValueError as exc:
            return f"[error] {exc}"
        perception = getattr(self, "_last_perception", None)
        base_path = getattr(perception, "screenshot_path", None) if perception else None
        if base_path is None or not Path(base_path).exists():
            return (
                "[error] No screenshot available to preview on. Wait for the "
                "first perception (or call windows__Screenshot) and retry."
            )
        # Convert normalized [0,1] → image pixels for the marker overlay.
        img_w = getattr(perception, "screenshot_width", 0) or 0
        img_h = getattr(perception, "screenshot_height", 0) or 0
        if img_w and img_h:
            img_px_pts = [(x * img_w, y * img_h) for x, y in pts]
        else:
            img_px_pts = pts  # fallback (should not happen in practice)
        try:
            marked = mark_points(Image.open(base_path), img_px_pts)
        except Exception as exc:
            return f"[error] Failed to draw preview markers: {exc}"
        out_path = self.config.cache_dir_absolute() / "preview_points.png"
        try:
            out_path.parent.mkdir(parents=True, exist_ok=True)
            marked.save(out_path, "PNG")
        except Exception as exc:
            return f"[error] Failed to save preview image: {exc}"
        # Store NORMALIZED coords (not image pixels) so follow-up text uses
        # the same space the model provides for windows__Click(loc=[x, y]).
        self._pending_preview = (out_path, pts)
        listing = ", ".join(
            f"marker {i + 1} at ({x:.4f}, {y:.4f})" for i, (x, y) in enumerate(pts)
        )
        return (
            f"Preview attached: {listing} (normalized [0,1] coordinates). "
            "The marked screenshot is shown below — check whether the markers "
            "sit on the target. Call PreviewPoints again with adjusted "
            "coordinates if needed (it replaces the previous markers); once a "
            "marker is right, click with windows__Click(loc=[x, y]) using "
            "those exact coordinates."
        )

    def _register_preview_points(self) -> None:
        """Register the PreviewPoints local function tool with the LLM."""
        self.llm.register_local_function(
            "PreviewPoints",
            self._preview_points_impl,
            schema=PREVIEW_POINTS_SCHEMA,
            description=(
                "Preview up to 3 candidate click coordinates BEFORE clicking: "
                "your best guesses in normalized [0,1] coordinates are drawn "
                "as numbered red markers on a clean copy of the screenshot and "
                "shown back to you. Use this as the LAST RESORT when neither "
                "UIA labels (windows__Snapshot) nor SoM markers "
                "(DesktopInteract) can locate the target: give your best 1-3 "
                "guesses, look at the markers, adjust if needed, then click "
                "with windows__Click(loc=[x, y]) using the confirmed "
                "coordinates."
            ),
        )

    # ------------------------------------------------------------------
    # CompleteTask
    # ------------------------------------------------------------------

    def _complete_task_impl(self, answer: str) -> str:
        """Handler for the CompleteTask tool: stash the final answer for run_task.

        The orchestrator checks ``self._pending_completion`` after the Think step
        and, when set, returns it directly and skips verification. The decision to
        finish (and to skip verify) is therefore the model's, made by choosing to
        call this tool.
        """
        self._pending_completion = answer
        return "Task marked as complete; returning your answer to the user."

    # ------------------------------------------------------------------
    # Wait
    # ------------------------------------------------------------------

    async def _wait_impl(self, seconds: float) -> str:
        """Pause execution for the given number of seconds.

        Useful for waiting on loading spinners, animations, UI transitions,
        download dialogs, or any situation where the screen needs to settle
        before the next perception. The wait is interruptible by the kill
        switch (checked every 0.5s).
        """
        seconds = max(0.5, min(float(seconds), 30.0))
        deadline = asyncio.get_event_loop().time() + seconds
        while asyncio.get_event_loop().time() < deadline:
            if self._check_cancelled():
                return "[error] Task cancelled by kill switch during wait."
            remaining = deadline - asyncio.get_event_loop().time()
            await asyncio.sleep(min(0.5, max(0.05, remaining)))
        return f"Waited {seconds:.1f}s."

    def _register_wait(self) -> None:
        """Register the Wait local function tool with the LLM."""
        self.llm.register_local_function(
            "Wait",
            self._wait_impl,
            schema=WAIT_SCHEMA,
            description=(
                "Pause for `seconds` (0.5–30.0) before the next action. Use "
                "when the screen is loading, an animation is playing, a dialog "
                "is appearing, or the UI needs time to settle. Always follow "
                "with a fresh perception to confirm the new state."
            ),
        )

    # ------------------------------------------------------------------
    # CompleteTask
    # ------------------------------------------------------------------

    def _complete_task_impl(self, answer: str) -> str:
        """Handler for the CompleteTask tool: stash the final answer for run_task.

        The orchestrator checks ``self._pending_completion`` after the Think step
        and, when set, returns it directly and skips verification. The decision to
        finish (and to skip verify) is therefore the model's, made by choosing to
        call this tool.
        """
        self._pending_completion = answer
        return "Task marked as complete; returning your answer to the user."

    def _register_complete_task(self) -> None:
        """Register the CompleteTask local function tool with the LLM."""
        self.llm.register_local_function(
            "CompleteTask",
            self._complete_task_impl,
            schema=COMPLETE_TASK_SCHEMA,
            description=(
                "Declare the task complete with a final answer. This ends "
                "the turn immediately (no verification) and returns the "
                "answer to the user. Only call when you are confident the "
                "task is done."
            ),
        )
