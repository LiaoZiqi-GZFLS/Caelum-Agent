"""ReAct loop orchestrator: Perceive → Reflect → Think → Act → Verify."""

from __future__ import annotations

import asyncio
import atexit
import base64
import json
import logging
import re
from pathlib import Path
from collections import deque
from typing import Any

import httpx
import openai

from agent.config import Config
from agent.content_writer import register_draft_content
from agent.file_reader import register_read_document
from agent.focus_guard import register_focus_guard
from agent.history_archive import HistoryArchiver
from agent.image_gen import register_generate_image
from agent.kill_switch import KillSwitch
from agent.media import parse_media_refs, register_view_media
from agent.kimi_memory import KimiMemoryClient
from agent.llm_client import LLMClient
from agent.memory import MemoryStore
from agent.perception import PerceptionModule
from agent.reflection import ReflectionEngine
from agent.security import SecurityGuard
from agent.self_window import register_self_window
from agent.skills import SkillLearner
from agent.state_machine import AgentStateMachine
from agent.task_list import TaskList, register_task_list
from agent.tools import (
    COMPLETE_TASK_SCHEMA,
    DESKTOP_INTERACT_SCHEMA,
    REQUEST_HUMAN_HELP_SCHEMA,
    UPGRADE_VISION_SCHEMA,
    register_all,
)
from agent.window_capture import register_capture_window
from eventbus import EventBus
from eventbus.events import (
    KillSwitchTriggered,
    LLMResponseReceived,
    ToolCallCompleted,
    ToolCallRequested,
    UserInputReceived,
)
from mcp_client import MCPMultiplexer, ToolResult


logger = logging.getLogger("caelum.orchestrator")


class TransientAPIError(Exception):
    """Raised when an LLM API call fails but the circuit breaker has not tripped."""


class APIBreakerTripped(Exception):
    """Raised when the API failure threshold is reached."""


# Windows-MCP tools that require a target (`loc` [x, y] or `label` from Snapshot).
# Calling them without either raises a server-side ValueError; we short-circuit
# client-side with an actionable error that points the model at Snapshot first.
_POSITIONAL_WINDOWS_TOOLS = frozenset({"Click", "Type", "Scroll", "Move"})

# windows-mcp errors when a label from an older Snapshot is used after a new
# Snapshot/Screenshot rebuilt the label space.
_STALE_LABEL_RE = re.compile(
    r"Label \d+ out of range|Failed to find element with label", re.IGNORECASE
)

# Local function tools that must pass the security guard before execution.
# Formula tools (cloud-side) and the other local tools are intentionally
# ungated: the former cannot touch the machine, the latter are the agent's
# own control surface. CodeRunner executes model-generated code locally, so
# it is held to the same standard as MCP write operations.
_LOCAL_TOOL_SECURITY = {"CodeRunner": "write_risky"}

# Loop budget: a task starts with 10 perception-action loops. Each time the
# budget is exhausted without completing, a reflection checkpoint asks the
# model whether its approach is fundamentally sound; a YES extends the budget
# by 10, up to a hard cap of 50 loops.
_INITIAL_LOOP_LIMIT = 10
_LOOP_LIMIT_INCREMENT = 10
_MAX_LOOP_LIMIT = 50

# If the model still hasn't created a task list by this loop, inject a one-time
# reminder. By loop 5 a task has clearly become multi-step, and a salient plan
# matters more as the context grows.
_TASK_LIST_NUDGE_LOOP = 5

# Argument keys whose values must never reach the audit log in clear text
# (e.g. a password typed via windows/Type).
_SENSITIVE_ARG_KEYS = frozenset(
    {"password", "passwd", "secret", "token", "api_key", "text"}
)

# Matches when the ENTIRE assistant text is a parroted tool call, e.g.
#   CompleteTask(answer='你好！')
# The model sometimes writes the call as plain text instead of invoking it
# through function calling. Only a full-content match counts, so an answer
# that merely mentions the syntax stays on the normal path.
_TEXT_COMPLETION_RE = re.compile(
    r"""^\s*CompleteTask\s*\(\s*answer\s*=\s*(['"])(.*?)\1\s*\)\s*$""",
    re.DOTALL,
)


def _parse_text_completion(content: str) -> str | None:
    """Extract the answer from a text-form ``CompleteTask(answer=...)``, else None."""
    if not content:
        return None
    m = _TEXT_COMPLETION_RE.match(content)
    return m.group(2) if m else None


class AgentOrchestrator:
    STATE_KEY = "orchestrator_state"

    def __init__(
        self,
        config: Config,
        eventbus: EventBus,
        llm: LLMClient,
        mcp: MCPMultiplexer,
        kill_switch: KillSwitch,
        perception: PerceptionModule | None = None,
        memory: MemoryStore | None = None,
        reflection: ReflectionEngine | None = None,
        security: SecurityGuard | None = None,
        skill_learner: SkillLearner | None = None,
    ) -> None:
        self.config = config
        self.eventbus = eventbus
        self.llm = llm
        self.mcp = mcp
        self.kill_switch = kill_switch
        self.ui_detector: Any | None = None
        self.state = AgentStateMachine(eventbus)
        self._kimi_client: Any | None = None
        if config.memory.use_kimi_memory or config.reflection.use_rethink:
            self._kimi_client = KimiMemoryClient(llm)
        self.memory = memory or MemoryStore(
            db_path=config.sqlite_path_absolute(),
            skills_dir=config.skills_dir_absolute(),
            vector_dir=config.cache_dir_absolute() / "chroma",
            audit_log_path=config.audit_log_absolute(),
            kimi=self._kimi_client,
        )
        self.reflection = reflection or ReflectionEngine(config, self.memory, kimi=self._kimi_client)
        self.perception = perception or PerceptionModule(config, mcp=mcp)
        self.security = security or SecurityGuard(
            config.security,
            confirm_callback=self._request_human_confirmation,
        )
        self.skill_learner = skill_learner or SkillLearner(
            skills_dir=config.skills_dir_absolute(),
            memory=self.memory,
            llm_client=self.llm,
            similarity_threshold=config.skills.similarity_threshold,
        )
        self.history: list[dict[str, Any]] = []
        # Model-managed task list (UpdateTaskList tool); re-injected into the
        # history every loop so long-task plans stay salient. Cleared at the
        # start of each run_task and self-clears when all items complete.
        self.task_list = TaskList()
        # Whether the one-time "consider a task list" nudge has fired this run.
        self._task_list_nudged = False
        # Loop-extension confirmation, merged into the next perception message.
        self._pending_loop_notice: str | None = None
        self.task_id: str | None = None
        self.current_instruction: str = ""
        self.last_action_summary: str = ""
        self.consecutive_action_failures = 0
        self.consecutive_api_failures = 0
        self.action_traces: list[str] = []
        self._cancel_event = asyncio.Event()
        self._recent_hashes: deque[str] = deque(
            maxlen=self.config.kill_switch.same_ui_loop_threshold
        )
        # Whether the current/last task used a screen-touching tool. Reset at
        # the start of each run_task; initialized here so helpers that inspect
        # it (e.g. tests calling _verify/_execute_tool_calls directly) never see
        # a missing attribute.
        self._used_ui_tool = False
        # When the model explicitly finishes via the CompleteTask tool, its
        # answer is stashed here and run_task returns it directly, skipping the
        # post-action perceive/verify/final-answer cycle. Reset each round; the
        # decision to skip verification is the model's (via the tool call), not a
        # hard-coded rule.
        self._pending_completion: str | None = None
        # Whether any tool call in the current ReAct round returned failure.
        # Reset at the top of each loop; _verify refuses to mark a round that
        # had a tool failure as COMPLETED (blocks hallucinated success).
        self._round_tool_failed = False
        # Signature of the last executed tool-call batch and whether it fully
        # succeeded, used to short-circuit an LLM that re-emits the exact same
        # batch on the next round (a recurring token-wasting loop).
        self._last_batch_signature: tuple[tuple[str, str], ...] | None = None
        self._last_batch_all_succeeded = False
        # Fire-and-forget background tasks (currently skill learning). Tracked so
        # they are not garbage-collected and so shutdown() can drain them.
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._human_confirm_callback: Any | None = None
        self._human_question_callback: Any | None = None
        # Whether a human is at the keyboard (TTY). main.py sets this from
        # sys.stdin.isatty(); piped/one-shot scripted runs get False so the
        # system prompt can steer the model away from human-in-the-loop tools.
        self._interactive: bool = True
        # Lazy-mode SoM follow-up: _desktop_interact_impl stashes the vision
        # perception it refreshed here; _think_and_act appends its annotated
        # screenshot to history right after the tool result so the model can
        # see the markers it is choosing among (main-loop perceptions carry no
        # SoM in lazy mode). Cleared after each append and at task start.
        self._pending_som_followup: Any | None = None
        # ViewMedia uploads: the tool result carries a "[media_ref] kind ms://"
        # marker; _execute_tool_calls lifts it here and _think_and_act appends
        # a real image_url/video_url content part after the tool results so the
        # model sees the actual media (merged with the SoM follow-up into one
        # user message to avoid consecutive same-role turns).
        self._pending_media_parts: list[dict[str, Any]] = []
        # UpgradeVision: the handler sets this flag; _think_and_act consumes it
        # by injecting a fresh 1080p perception right after the tool result.
        self._upgrade_requested: bool = False
        # Set by initialize() when the corresponding tools are enabled; the
        # task-end finally block fire-and-forgets their remote sweeps.
        self.file_extractor: Any | None = None
        self.media_uploader: Any | None = None
        self.self_window: Any | None = None
        self.focus_guard: Any | None = None
        self.eventbus.subscribe("KillSwitchTriggered", self._on_kill_switch)

    def set_human_confirmation_callback(self, callback: Any) -> None:
        self._human_confirm_callback = callback

    def set_human_question_callback(self, callback: Any) -> None:
        self._human_question_callback = callback

    def set_interactive(self, interactive: bool) -> None:
        self._interactive = interactive

    def _request_human_confirmation(self, summary: str, action: dict[str, Any]) -> bool:
        if self._human_confirm_callback is not None:
            return self._human_confirm_callback(summary, action)
        # Default to deny if no handler is registered.
        return False

    async def _on_kill_switch(self, event: Any) -> None:
        if isinstance(event, KillSwitchTriggered):
            self._cancel_event.set()

    def _check_cancelled(self) -> bool:
        if self._cancel_event.is_set():
            return True
        return False

    async def initialize(self) -> None:
        self.config.cache_dir_absolute().mkdir(parents=True, exist_ok=True)
        await self.llm.initialize()
        await self.mcp.connect_all()
        register_all(
            self.llm,
            self.mcp,
            code_cwd=str(self.config.cache_dir_absolute()),
            # Unsandboxed JavaScript is only offered when the user opted into
            # auto-approval (--yes / --yes-all both set auto_approve).
            allow_javascript=self.security.auto_approve,
        )
        self._register_desktop_interact()
        self._register_upgrade_vision()
        self._register_complete_task()
        self._register_human_help()
        register_task_list(self.llm, self.task_list)
        extractor = register_read_document(
            self.llm, self.config.llm, self.config.cache_dir_absolute()
        )
        self.file_extractor = extractor
        self.media_uploader = register_view_media(
            self.llm, self.config.llm, self.config.cache_dir_absolute()
        )
        # Fire-and-forget quota sweep: the platform keeps uploads forever and
        # the per-read delete is only best-effort. Only scheduled when the LLM
        # client shares its real httpx pool (production); fakes have no .http
        # and unit tests stay hermetic.
        if getattr(self.llm, "http", None) is not None:
            for sweeper in (self.file_extractor, self.media_uploader):
                if sweeper is None:
                    continue
                sweep_task = asyncio.create_task(sweeper.sweep_remote())
                self._background_tasks.add(sweep_task)
                sweep_task.add_done_callback(self._background_tasks.discard)
        register_draft_content(
            self.llm,
            self.config.cache_dir_absolute() / "drafts",
            doc_resolver=extractor.read_by_ref if extractor is not None else None,
        )
        # GenerateImage reuses the media uploader for its visual self-review;
        # it is unavailable (not registered) when media upload is disabled.
        register_generate_image(
            self.llm,
            self.config.llm,
            self.config.cache_dir_absolute(),
            uploader=self.media_uploader,
        )
        register_capture_window(
            self.llm,
            self.config.llm,
            self.config.cache_dir_absolute(),
            uploader=self.media_uploader,
        )
        self.self_window = register_self_window(self.llm)
        # Guardrail: a hidden console must never outlive the process, or the
        # user is left with an invisible agent they cannot see or stop.
        atexit.register(self._restore_console)
        self.focus_guard = register_focus_guard(self.llm)
        if self.config.ui_detector.enabled:
            from ui_detector import UIDetector

            self.ui_detector = UIDetector(self.config.ui_detector)
            if not self.config.ui_detector.lazy:
                # Eager mode: load the model at startup and run vision on every
                # perception (legacy behaviour, useful for pure-vision tasks).
                self.ui_detector.load()
            elif self.config.ui_detector.preload:
                # Warm load + lazy inference: keep the model resident so the first
                # DesktopInteract doesn't stall, while perceive() still skips
                # annotate. Costs resident memory for a fast first click.
                self.ui_detector.load()
            # Lazy mode (default): the model loads on first predict/annotate,
            # so compute/filesystem/API tasks never pay the load cost.
            self.perception.ui_detector = self.ui_detector
        self._load_state()
        self.kill_switch.start()

    async def shutdown(self) -> None:
        # Let in-flight skill learning finish (best-effort, bounded) so a just-
        # completed task's skill isn't lost on a clean exit. The user already has
        # their answer by now; this only delays process teardown, never the reply.
        if self._background_tasks:
            pending = list(self._background_tasks)
            try:
                await asyncio.wait_for(asyncio.gather(*pending), timeout=45.0)
            except asyncio.TimeoutError:
                for task in pending:
                    if not task.done():
                        task.cancel()
            except asyncio.CancelledError:
                raise
        self._save_state()
        self.kill_switch.stop()
        await self.mcp.disconnect_all()
        await self.llm.close()
        self.perception.shutdown()
        if self.ui_detector is not None:
            self.ui_detector.shutdown()

    def _save_state(self) -> None:
        # Deliberately excludes `history`: it embeds base64 screenshots (tens
        # of MB per long task), and run_task rebuilds it from scratch anyway,
        # so a restored history was never read — pure disk cost.
        payload = {
            "state": self.state.current_state,
            "task_id": self.task_id,
            "current_instruction": self.current_instruction,
            "consecutive_action_failures": self.consecutive_action_failures,
            "consecutive_api_failures": self.consecutive_api_failures,
        }
        self.memory.set_state(self.STATE_KEY, json.dumps(payload, ensure_ascii=False))
        logger.info("Orchestrator state saved")

    def _load_state(self) -> None:
        raw = self.memory.get_state(self.STATE_KEY)
        if not raw:
            return
        try:
            payload = json.loads(raw)
            saved_state = payload.get("state", "IDLE")
            # Do not restore terminal/error states into a fresh process; start clean.
            if saved_state in {"COMPLETED", "ERROR", "STUCK"}:
                self.consecutive_action_failures = 0
                self.consecutive_api_failures = 0
                self.history = []
                self.current_instruction = ""
                self._save_state()
                logger.info("Orchestrator state reset from terminal state: %s", saved_state)
                return
            self.consecutive_action_failures = payload.get("consecutive_action_failures", 0)
            self.consecutive_api_failures = payload.get("consecutive_api_failures", 0)
            # History is never restored (see _save_state); ignore the legacy
            # field if an older payload still carries one.
            self.history = []
            self.current_instruction = payload.get("current_instruction", "")
            if saved_state != "IDLE":
                asyncio.create_task(
                    self.state.transition(saved_state, task_id=self.task_id)
                )
            logger.info("Orchestrator state restored")
        except Exception as exc:
            logger.warning("Failed to restore orchestrator state: %s", exc)

    async def _desktop_interact_impl(
        self, label: int, action: str, text: str | None = None
    ) -> str:
        """Convert a SoM label to screen coordinates and execute the action.

        Look up the label in the most recent perception's som_annotations,
        convert normalized coordinates to screen pixels, then call the
        appropriate Windows-MCP tool.
        """
        # Refresh SoM from the latest screenshot so labels map to the current
        # screen. This is the single on-demand vision entry point; in lazy mode
        # it is also what triggers model loading on first use.
        if self.ui_detector is not None and self.config.ui_detector.enabled:
            self._last_perception = await self.perception.perceive_with_vision(
                self.current_instruction
            )
            # In lazy mode the main-loop perceptions carry no SoM annotations,
            # so without this the model picks DesktopInteract labels blind.
            # Stash the fresh vision perception; _think_and_act appends its
            # annotated screenshot to history right after this tool's result.
            if self.config.ui_detector.lazy:
                self._pending_som_followup = self._last_perception
        perception = getattr(self, "_last_perception", None)
        if perception is None:
            return "[error] No perception data available. Run perception first."

        # Find the annotation with the matching label.
        match = None
        for ann in perception.som_annotations:
            if ann.get("label") == label:
                match = ann
                break
        if match is None:
            available = [a.get("label") for a in perception.som_annotations]
            return f"[error] SoM label {label} not found. Available labels: {available}"

        # Convert normalized [0,1] to screen pixel coordinates.
        sw = perception.screen_width or 1920
        sh = perception.screen_height or 1080
        screen_x = int(round(match.get("center_x", 0) * sw))
        screen_y = int(round(match.get("center_y", 0) * sh))

        is_uncertain = match.get("verdict") == "uncertain"

        if action in ("click", "double_click", "right_click"):
            mcp_action = "Click"
            mcp_args: dict[str, Any] = {"loc": [screen_x, screen_y]}
            if action == "double_click":
                mcp_args["times"] = 2
            elif action == "right_click":
                mcp_args["button"] = "right"
        elif action == "type":
            # Type: click first to focus, then type.
            if self._check_cancelled():
                return "[error] Task cancelled by kill switch."
            focus_result = await self.mcp.call("windows", "Click", {"loc": [screen_x, screen_y]})
            if not focus_result.success:
                return f"[error] Failed to focus element at ({screen_x}, {screen_y}): {focus_result.content}"
            if self._check_cancelled():
                return "[error] Task cancelled by kill switch."
            type_result = await self.mcp.call("windows", "Type", {"text": text or ""})
            if type_result.success:
                msg = f"OK: typed text at ({screen_x}, {screen_y}) — {type_result.content[:200]}"
                if is_uncertain:
                    msg = "[uncertain] " + msg + " (Verifier was unsure about this element; verify the result.)"
                return msg
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
                msg = f"OK: {action} at ({screen_x}, {screen_y}) — {scroll_result.content[:200]}"
                if is_uncertain:
                    msg = "[uncertain] " + msg + " (Verifier was unsure about this element; verify the result.)"
                return msg
            return f"[error] {scroll_result.content}"
        else:
            return f"[error] Unknown action: {action}"

        if self._check_cancelled():
            return "[error] Task cancelled by kill switch."
        result = await self.mcp.call("windows", mcp_action, mcp_args)
        if result.success:
            msg = f"OK: {action} at ({screen_x}, {screen_y}) — {result.content[:200]}"
            if is_uncertain:
                msg = "[uncertain] " + msg + " (Verifier was unsure about this element; verify the result.)"
            return msg
        return f"[error] {result.content}"

    async def _upgrade_vision_impl(self) -> str:
        """Handler for UpgradeVision: raise the screenshot cap to 1080p.

        Sets the perception override so every subsequent screenshot (main-loop
        and SoM passes) compresses to the upgraded size, and flags
        _think_and_act to inject a fresh high-res perception right after this
        tool result so the model can continue without wasting a round.
        """
        cfg = self.config.screenshot
        self.perception.max_size_override = (
            cfg.upgraded_max_width, cfg.upgraded_max_height
        )
        self._upgrade_requested = True
        return (
            f"[ok] Vision upgraded to {cfg.upgraded_max_width}x"
            f"{cfg.upgraded_max_height} for the rest of this task. A fresh "
            "high-resolution screenshot follows."
        )

    def _register_upgrade_vision(self) -> None:
        """Register the UpgradeVision local function tool with the LLM."""
        self.llm.register_local_function(
            "UpgradeVision",
            self._upgrade_vision_impl,
            schema=UPGRADE_VISION_SCHEMA,
            description=(
                "Upgrade screenshot resolution from 720p to 1080p for the rest "
                "of this task. Use when you repeatedly cannot read small text "
                "or locate elements in the screenshot. A fresh high-resolution "
                "screenshot is attached right after the call."
            ),
        )

    def _register_desktop_interact(self) -> None:
        """Register the DesktopInteract local function tool with the LLM."""
        self.llm.register_local_function(
            "DesktopInteract",
            self._desktop_interact_impl,
            schema=DESKTOP_INTERACT_SCHEMA,
            description=(
                "Interact with a UI element identified by a SoM (Set-of-Mark) label number "
                "from the annotated screenshot (numbered red circles on detected elements). "
                "This is VISION-based: it works on ANY app, including ones whose UIA tree is "
                "missing, empty, or inaccurate (Qt apps like WeChat/QQ, Electron apps, games, "
                "custom-drawn controls). PREFER this over windows__Click/Type whenever a SoM "
                "marker is visible on your target, and fall back to it when windows__Snapshot "
                "shows no usable element or label-based clicks land wrong. "
                "Actions: click, double_click, right_click, type (needs text=), scroll_down/up."
            ),
        )

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
                "Finish the task and return `answer` to the user, SKIPPING the "
                "verification step. Call this ONLY for purely conversational turns "
                "(greetings, thanks, 'what can you do') or when you are certain no "
                "screen/file action was needed and nothing needs verifying. For "
                "tasks that changed the screen or files, do NOT call this — give a "
                "normal final answer instead so the result can be verified."
            ),
        )

    async def _request_human_help_impl(self, question: str, options: list) -> str:
        """Handler for RequestHumanHelp: ask the human and return their answer.

        The call itself is the pause: the ReAct loop blocks here (same thread
        model as confirm_interactive) with full history intact, the state
        machine shows WAITING_HUMAN, and the answer goes back to the model as
        the tool result. None from the callback means the human cancelled
        (ESC/Ctrl+C) or no human is present (non-TTY).
        """
        options = [str(o).strip() for o in (options or []) if str(o).strip()]
        if not question or not (2 <= len(options) <= 4):
            return "[error] RequestHumanHelp requires a question and 2-4 options."
        callback = self._human_question_callback
        if callback is None:
            return (
                "[unavailable] No human is present to answer. End the task and "
                "explain what the user must do manually."
            )
        # The human is about to be asked something: the console must be visible
        # even if the model hid it with SelfWindow earlier.
        self._restore_console()
        await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
        try:
            answer = callback(question, options)
        except Exception as exc:
            logger.warning("human question callback failed: %s", exc)
            answer = None
        finally:
            await self.state.transition("EXECUTING", task_id=self.task_id)
        if answer is None:
            return "[cancelled] The human dismissed the question without answering."
        return f"Human answered: {answer}"

    def _register_human_help(self) -> None:
        """Register the RequestHumanHelp local function tool with the LLM."""
        self.llm.register_local_function(
            "RequestHumanHelp",
            self._request_human_help_impl,
            schema=REQUEST_HUMAN_HELP_SCHEMA,
            description=(
                "Ask the human to perform a step you cannot do yourself (login, "
                "scan a QR code, solve a CAPTCHA, enter a 2FA code, OS permission "
                "dialog). The CLI shows your question with the given options plus "
                "a free-text option and returns the human's answer. Prefer this "
                "over retrying an action that keeps failing because it requires "
                "human involvement."
            ),
        )

    @staticmethod
    def _format_perception(perception: Any) -> list[dict[str, Any]]:
        """Convert a Perception dataclass into a multimodal message for the LLM."""
        text_parts = [perception.description]
        if perception.som_annotations:
            text_parts.append(
                "SoM annotations (numbered markers on screenshot):\n"
                + "\n".join(
                    f"  [{a.get('label', '?')}] at ({a.get('center_x', 0):.3f}, {a.get('center_y', 0):.3f})"
                    + (f" score={a.get('score', 0):.2f}" if a.get('score') else "")
                    for a in perception.som_annotations
                )
            )
            text_parts.append(
                "To interact with an element, call DesktopInteract(label=<number>, action=<action>). "
                "Actions: click, double_click, right_click, type (needs text=), scroll_down, scroll_up."
            )

        content: list[dict[str, Any]] = [
            {"type": "text", "text": "\n\n".join(text_parts)},
        ]

        # Prefer the SoM-annotated screenshot if it exists; fall back to raw.
        image_path: Path | None = None
        for candidate in (
            perception.annotated_screenshot_path,
            perception.screenshot_path,
        ):
            if candidate is not None and candidate.exists():
                image_path = candidate
                break
        if image_path is not None and image_path.exists():
            try:
                image_bytes = image_path.read_bytes()
                b64 = base64.b64encode(image_bytes).decode("utf-8")
                content.append({
                    "type": "image_url",
                    "image_url": {"url": f"data:image/jpeg;base64,{b64}"},
                })
            except Exception as exc:
                content.append({
                    "type": "text",
                    "text": f"[Could not include screenshot: {exc}]",
                })
        else:
            content.append({
                "type": "text",
                "text": "Screenshot not available.",
            })

        return content

    @staticmethod
    def _format_som_followup(perception: Any) -> list[dict[str, Any]] | None:
        """Build the lazy-mode SoM follow-up message for DesktopInteract.

        DesktopInteract refreshes vision perception internally; in lazy mode
        the main loop never sends the annotated image, so the model would pick
        labels blind. This message — appended right after the tool result —
        carries the annotated screenshot plus the label list so subsequent
        DesktopInteract calls are visually grounded. Returns None when there
        is nothing worth showing (no annotations or no readable image).
        """
        if not perception.som_annotations:
            return None
        image_path: Path | None = None
        for candidate in (
            perception.annotated_screenshot_path,
            perception.screenshot_path,
        ):
            if candidate is not None and candidate.exists():
                image_path = candidate
                break
        if image_path is None:
            return None
        try:
            b64 = base64.b64encode(image_path.read_bytes()).decode("utf-8")
        except Exception:
            return None
        labels = "\n".join(
            f"  [{a.get('label', '?')}] at ({a.get('center_x', 0):.3f}, "
            f"{a.get('center_y', 0):.3f})"
            for a in perception.som_annotations
        )
        return [
            {
                "type": "text",
                "text": (
                    "SoM-annotated screenshot from the DesktopInteract detection "
                    "pass (numbered red markers = detected candidates):\n"
                    f"{labels}\n"
                    "Use these label numbers for subsequent DesktopInteract "
                    "calls. Each call re-detects, so after the screen changes "
                    "rely on the latest annotated image."
                ),
            },
            {"type": "image_url", "image_url": {"url": f"data:image/jpeg;base64,{b64}"}},
        ]

    async def _llm_chat_with_breaker(
        self,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = ...,
    ) -> Any:
        """Call the LLM with API-failure counting and circuit breaker logic."""
        try:
            completion = await self.llm.chat(messages, tools)
        except (openai.APIError, httpx.HTTPError, asyncio.TimeoutError) as exc:
            self.consecutive_api_failures += 1
            if self.consecutive_api_failures >= self.config.kill_switch.api_failure_threshold:
                await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
                raise APIBreakerTripped(
                    "Too many consecutive API failures; switched to local-only mode."
                ) from exc
            raise TransientAPIError(str(exc)) from exc
        except Exception as exc:
            # Treat any remaining exception as transient for robustness; the
            # outer loop will record it and decide whether to continue.
            logger.warning(
                "Unexpected exception type during LLM call: %s: %s",
                type(exc).__name__,
                exc,
            )
            self.consecutive_api_failures += 1
            if self.consecutive_api_failures >= self.config.kill_switch.api_failure_threshold:
                await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
                raise APIBreakerTripped(
                    "Too many consecutive API failures; switched to local-only mode."
                ) from exc
            raise TransientAPIError(str(exc)) from exc
        else:
            self.consecutive_api_failures = 0
            return completion

    def _is_same_ui_loop(self, ui_hash: str) -> bool:
        """Return True if the UI has stayed identical for the configured threshold."""
        if self._recent_hashes and ui_hash == self._recent_hashes[-1]:
            self._recent_hashes.append(ui_hash)
        else:
            self._recent_hashes.clear()
            self._recent_hashes.append(ui_hash)
        return len(self._recent_hashes) >= self.config.kill_switch.same_ui_loop_threshold

    def _restore_console(self) -> None:
        """Guardrail: make the agent's own console visible again.

        Called when a task ends, before asking the human anything, and via
        atexit — hiding the console (SelfWindow) must always be reversible.
        """
        win = self.self_window
        if win is None:
            return
        try:
            win.show()
        except Exception:
            pass

    async def run_task(self, user_input: str, task_id: str | None = None) -> str:
        """Run one task and archive its history to data/archives/ on exit."""
        tid = task_id or "task-0"
        outcome = "ok"
        try:
            outcome = await self._run_task_impl(user_input, tid)
            return outcome
        finally:
            try:
                archives_dir = self.config.cache_dir_absolute().parent / "archives"
                HistoryArchiver(archives_dir, _SENSITIVE_ARG_KEYS).archive(
                    task_id=self.task_id,
                    instruction=user_input,
                    outcome=str(outcome)[:500],
                    history=self.history,
                )
            except Exception as exc:  # archiving must never break the agent
                logger.warning("Failed to archive history: %s", exc)
            # Guardrails: never leave the console hidden or the focus watchdog
            # running after a task — it would fight the user for focus.
            self._restore_console()
            if self.focus_guard is not None:
                try:
                    await self.focus_guard.stop()
                except Exception as exc:
                    logger.debug("Failed to stop focus guard: %s", exc)
            # Task-end quota sweep: ms:// media references are only valid for
            # this task's history, and file-extract leftovers are throwaway,
            # so all remote uploads are stale once the task is done.
            for sweeper in (self.file_extractor, self.media_uploader):
                if sweeper is None:
                    continue
                sweep_task = asyncio.create_task(sweeper.sweep_remote())
                self._background_tasks.add(sweep_task)
                sweep_task.add_done_callback(self._background_tasks.discard)

    async def _run_task_impl(self, user_input: str, task_id: str) -> str:
        self.task_id = task_id
        self.current_instruction = user_input
        self._cancel_event.clear()
        self.consecutive_action_failures = 0
        self.consecutive_api_failures = 0
        self._last_batch_signature = None
        self._last_batch_all_succeeded = False
        # Ensure a fresh operational state for each new task.
        if self.state.current_state != "IDLE":
            await self.state.transition("IDLE", task_id=self.task_id)
        await self.eventbus.emit(UserInputReceived(text=user_input, task_id=self.task_id))
        await self.state.transition("PLANNING", task_id=self.task_id)

        reflection_context = self.reflection.build_context(user_input)
        skill_matches = self.memory.search_skills(user_input, top_k=2)
        skill_context = ""
        if skill_matches:
            skill_context = "Relevant skills:\n" + "\n".join(
                f"- {s['name']}: {s['content'][:500]}" for s in skill_matches
            )

        if self._interactive:
            exec_context = (
                "## Execution context\n"
                "A human is at the keyboard (interactive terminal). Use "
                "RequestHumanHelp when a step needs human involvement.\n\n"
            )
        else:
            exec_context = (
                "## Execution context\n"
                "This run is non-interactive (piped or scripted input): no human "
                "can answer questions or confirmations. Do NOT call "
                "RequestHumanHelp — it can only come back cancelled. If a step "
                "needs a human (login, CAPTCHA, 2FA codes, OS permission "
                "dialogs), finish with a normal text answer explaining exactly "
                "what the user must do manually.\n\n"
            )

        system_content = (
            "You are Caelum-Agent, a Windows desktop automation assistant. "
            "Use the provided tools to interact with the browser and desktop. "
            "Always explain your reasoning briefly before acting.\n\n"
            "## Working with the SoM (Set-of-Mark) screenshot\n"
            "The screenshot contains numbered red circle markers on detected UI elements. "
            "Each marker has a number (1, 2, 3, ...). To interact with a marked element:\n"
            "- Use DesktopInteract(label=N, action='click') to click marker N\n"
            "- Use DesktopInteract(label=N, action='type', text='...') to type into an input field\n"
            "- Use DesktopInteract(label=N, action='scroll_down') to scroll at marker N\n"
            "- For browser elements with refs (like e12), use playwright__browser_click(target='e12') instead.\n"
            "- For unmarked elements, use the raw MCP tools with explicit coordinates or refs.\n\n"
            "## Working with desktop (Windows-MCP) tools\n"
            "Before clicking or typing in a desktop app you MUST call windows__Snapshot first to "
            "get the target element's [id], then pass it as `label`:\n"
            "- windows__Click(label=<id>)  — never call Click with no loc/label.\n"
            "- windows__Type(text='...', label=<id>)  — Type with no loc/label fails.\n"
            "Example: Snapshot shows [5] Edit 'Text Editor' -> Type(text='hello', label=5).\n"
            "WHEN UIA FAILS, SWITCH TO VISION: windows__Snapshot relies on the app's UIA tree, "
            "which is often missing, empty, or inaccurate for Qt apps (WeChat/QQ), Electron apps, "
            "and custom-drawn controls. If Snapshot shows no element matching your target, its "
            "labels look wrong (clicking a label hits the wrong element), or a label expires "
            "('out of range') after re-snapshotting, STOP fighting UIA and use "
            "DesktopInteract(label=N, ...) with the SoM marker on your target instead — it is "
            "vision-based and works on any app. If you cannot see inside the app at all (empty "
            "tree AND no clear screenshot), call CaptureWindow(title) to view it directly.\n\n"
            "## Working files\n"
            f"Save every intermediate or scratch file (page snapshots, scraped "
            f"content, temp JSON/CSV/Markdown, downloaded artifacts) under "
            f"{self.config.cache_dir_absolute()} — never in the project root or "
            "the current working directory. Only write outside that directory "
            "when the user explicitly asks for a file at a specific path.\n\n"
            + exec_context +
            "## Asking the human for help\n"
            "If a step needs a human — login, scanning a QR code, CAPTCHA, SMS/2FA "
            "codes, OS permission dialogs — call RequestHumanHelp(question, options) "
            "instead of retrying the failing action. Make the question specific (name "
            "the site or app) and give 2-4 options; the CLI always adds a free-text "
            "'type something' option, so never include one yourself.\n"
            "Reading the answer: if the human completed the step, look at the screen "
            "again and continue the original plan. If they could not complete it or "
            "the answer is unclear, stop and finish with a normal text answer that "
            "explains where the task is blocked and what the user must do manually.\n\n"
            "## Finishing a turn\n"
            "- If the request is purely conversational (a greeting, thanks, or a "
            "question about your capabilities) and needs no screen or file action, "
            "finish by INVOKING the CompleteTask tool with your reply in its `answer` "
            "argument — a real function call through the tool-calling interface. Do "
            "not write the tool call out as plain text, do not call any other tool, "
            "and do not add a separate text answer.\n"
            "- If the task changes the screen or files, finish with a normal text "
            "answer (no CompleteTask) so the result is verified.\n"
            "- Browser/website tasks (open a site, read a page, fill a web form) must "
            "use the Playwright tools: playwright__browser_navigate to open the URL, "
            "playwright__browser_snapshot to read the page, playwright__browser_click / "
            "playwright__browser_type to interact. Do NOT click the desktop/taskbar "
            "browser icon for these tasks; only use desktop-level control when the "
            "user explicitly asks for it (e.g. 'open the browser app')."
        )
        if reflection_context:
            system_content += "\n\n" + reflection_context
        if skill_context:
            system_content += "\n\n" + skill_context

        self.history = [
            {"role": "system", "content": system_content},
            {"role": "user", "content": user_input},
        ]
        self._recent_hashes.clear()
        self._last_perception: Any | None = None
        self._pending_som_followup = None
        self._pending_media_parts = []
        self._upgrade_requested = False
        self.perception.max_size_override = None
        self.task_list.clear()
        self._task_list_nudged = False
        self._pending_loop_notice = None
        self.action_traces = []
        # Tracks whether this task has invoked any tool that touches the screen
        # (windows/playwright MCP, or the desktop_interact local tool). Pure
        # compute/API/filesystem tasks never set it, so the same-UI-loop guard
        # and the UI-change verification do not apply to them.
        self._used_ui_tool = False

        loop = 0
        loop_limit = _INITIAL_LOOP_LIMIT
        while True:
            if loop >= loop_limit:
                # Budget exhausted: a reflection checkpoint decides whether the
                # approach is sound enough to earn more loops (up to the cap),
                # otherwise the task stops here as stuck.
                if self.state.current_state in {"COMPLETED", "ERROR"}:
                    break
                try:
                    new_limit = await self._maybe_extend_loop_limit(loop_limit)
                except APIBreakerTripped as exc:
                    await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
                    return str(exc)
                if new_limit > loop_limit:
                    loop_limit = new_limit
                    continue
                await self.state.transition("STUCK", task_id=self.task_id)
                await self.reflection.record(
                    task_summary=user_input,
                    failure_reason="Exceeded maximum loop count",
                    fix_action="Break task into smaller steps.",
                )
                return "Agent reached the loop limit without completing the task."
            loop += 1
            if self._check_cancelled():
                await self.state.transition("IDLE", task_id=self.task_id)
                return "Task cancelled by kill switch."

            if self.consecutive_action_failures >= self.config.kill_switch.action_failure_threshold:
                await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
                return "Too many consecutive action failures; waiting for human guidance."

            if self.consecutive_api_failures >= self.config.kill_switch.api_failure_threshold:
                await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
                return "Too many consecutive API failures; switched to local-only mode."

            if self.state.current_state in {"COMPLETED", "ERROR", "STUCK"}:
                break

            await self.state.transition("EXECUTING", task_id=self.task_id)
            self._round_tool_failed = False
            self._pending_completion = None
            perception = await self.perception.perceive(
                instruction=self.current_instruction,
                with_vision=not self.config.ui_detector.lazy,
            )
            self._last_perception = perception
            perception_content = self._format_perception(perception)
            # Any extra context for this round (loop-extension notice, task
            # list render, or the one-time planning nudge) rides as extra text
            # parts on the SAME user message: Kimi rejects back-to-back
            # same-role messages, and these paths would otherwise append two
            # consecutive user turns.
            if self._pending_loop_notice is not None:
                perception_content.append(
                    {"type": "text", "text": self._pending_loop_notice}
                )
                self._pending_loop_notice = None
            # Keep the model-managed task list salient: re-inject a compact
            # render every loop so the plan isn't buried under tool results.
            if self.task_list.items:
                perception_content.append(
                    {"type": "text", "text": self.task_list.render()}
                )
            elif (
                loop >= _TASK_LIST_NUDGE_LOOP
                and not self._task_list_nudged
            ):
                # One-time nudge: the task has clearly become multi-step but
                # the model never planned. Fires once per run_task.
                self._task_list_nudged = True
                perception_content.append({
                    "type": "text",
                    "text": (
                        "You are several loops into this task without a plan. "
                        "Consider calling UpdateTaskList to break the remaining "
                        "work into concrete steps and track their status."
                    ),
                })
            self.history.append({"role": "user", "content": perception_content})

            # Check for total rejection by verifier (all candidates blocked).
            if perception.blocked_count > 0 and not perception.som_annotations:
                reason = f"Verifier rejected all {perception.blocked_count} candidates"
                await self.reflection.record(
                    task_summary=user_input,
                    failure_reason=reason,
                    fix_action="Retry detection with a different instruction or ask for human guidance.",
                )
                self.history.append({
                    "role": "user",
                    "content": (
                        f"{reason}. The UI may have changed or the target element may not be visible. "
                        "Try a different approach or describe what you are looking for differently."
                    ),
                })
                await self.state.transition("REFLECT", task_id=self.task_id)
                reflection_text = await self._reflect()
                await self.state.transition("PLANNING", task_id=self.task_id)
                continue

            if self._used_ui_tool and self._is_same_ui_loop(perception.ui_hash):
                loops = len(self._recent_hashes)
                reason = f"UI state unchanged for {loops} loops"
                await self.reflection.record(
                    task_summary=user_input,
                    failure_reason=reason,
                    fix_action="Change the approach or ask for human guidance.",
                )
                await self.state.transition("STUCK", task_id=self.task_id)
                return f"{reason}; agent is stuck."

            current_perception = perception
            self._last_perception = perception

            try:
                response = await self._think_and_act()
                if self.state.current_state == "COMPLETED":
                    return response

                if self._check_cancelled():
                    await self.state.transition("IDLE", task_id=self.task_id)
                    return "Task cancelled by kill switch."

                # Model-decided fast path: the model called CompleteTask(answer),
                # explicitly finishing and opting out of verification. Honor its
                # decision and return the answer without the post-action perceive
                # / _verify / _final_answer cycle. If the model also acted this
                # round (action_traces non-empty), still learn a skill from it.
                if self._pending_completion is not None:
                    answer = self._pending_completion
                    self._pending_completion = None
                    await self.state.transition("COMPLETED", task_id=self.task_id)
                    if self.action_traces:
                        self._schedule_skill_learning()
                    return answer

                # Capture the post-action perception for state-based verification.
                post_action_perception = await self.perception.perceive(
                    instruction=self.current_instruction,
                    with_vision=not self.config.ui_detector.lazy,
                )
                self._last_perception = post_action_perception
                self.history.append({
                    "role": "user",
                    "content": self._format_perception(post_action_perception),
                })

                # Verify by asking the model to reflect on last action success.
                await self.state.transition("VERIFYING", task_id=self.task_id)
                if await self._verify(current_perception, post_action_perception):
                    self.consecutive_action_failures = 0
                    final_answer = await self._final_answer()
                    if self.state.current_state == "COMPLETED":
                        self._schedule_skill_learning()
                        return final_answer
                    if self.state.current_state == "ERROR":
                        return final_answer
                    await self.state.transition("PLANNING", task_id=self.task_id)
                else:
                    await self.state.transition("REFLECT", task_id=self.task_id)
                    reflection = await self._reflect()
                    await self.reflection.record(
                        task_summary=user_input,
                        failure_reason="Verification failed",
                        fix_action=reflection,
                    )
                    # NOTE: _reflect() already appends the assistant message to
                    # history; appending it again here produced two consecutive
                    # assistant messages, which Kimi rejects with HTTP 400.
                    await self.state.transition("PLANNING", task_id=self.task_id)
            except TransientAPIError as exc:
                await self.reflection.record(
                    task_summary=user_input,
                    failure_reason=str(exc),
                    fix_action="Wait for API recovery and retry.",
                )
                await self.state.transition("PLANNING", task_id=self.task_id)
                continue
            except APIBreakerTripped as exc:
                await self.state.transition("WAITING_HUMAN", task_id=self.task_id)
                return str(exc)
            except Exception as exc:
                self.consecutive_action_failures += 1
                await self.reflection.record(
                    task_summary=user_input,
                    failure_reason=str(exc),
                    fix_action="Review the error and retry.",
                )
                await self.state.transition("ERROR", task_id=self.task_id)
                return f"Error during execution: {exc}"

        return "Task finished."

    async def _think_and_act(self) -> str:
        while True:
            completion = await self._llm_chat_with_breaker(self.history)
            message = completion.choices[0].message
            content = message.content or ""
            tool_calls = getattr(message, "tool_calls", None) or []

            await self.eventbus.emit(
                LLMResponseReceived(
                    content=content,
                    tool_calls=[c.model_dump() for c in tool_calls],
                    task_id=self.task_id,
                )
            )

            if not tool_calls:
                self.history.append({"role": "assistant", "content": content})
                text_answer = _parse_text_completion(content)
                if text_answer is not None:
                    # The model wrote CompleteTask(...) as plain text instead of
                    # invoking the tool. Honor it as a real call: stash the answer
                    # so run_task takes the fast path (no verify / final answer).
                    logger.info(
                        "Model emitted CompleteTask as text; treating as a tool call."
                    )
                    self._pending_completion = text_answer
                return content

            self.history.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [c.model_dump() for c in tool_calls],
            })

            tool_results = await self._execute_tool_calls(tool_calls)
            self.history.extend(tool_results)
            # Post-tool user turn, merging two follow-up sources into a single
            # message (Kimi rejects consecutive same-role turns):
            # 1. ViewMedia ms:// media parts — the model sees the actual media.
            # 2. Lazy-mode SoM follow-up — the annotated screenshot from the
            #    DesktopInteract detection pass (stashed by
            #    _desktop_interact_impl), so the model sees the markers it is
            #    choosing among.
            followup_parts: list[dict[str, Any]] = []
            if self._pending_media_parts:
                followup_parts.extend(self._pending_media_parts)
                self._pending_media_parts = []
                followup_parts.append({
                    "type": "text",
                    "text": "[ViewMedia] The media above was uploaded and "
                            "attached for your reference.",
                })
            if self._upgrade_requested:
                # UpgradeVision: take a fresh 1080p perception immediately so
                # the model can continue this turn with sharper eyes.
                self._upgrade_requested = False
                fresh = await self.perception.perceive(
                    instruction=self.current_instruction,
                    with_vision=not self.config.ui_detector.lazy,
                )
                self._last_perception = fresh
                followup_parts.extend(self._format_perception(fresh))
                followup_parts.append({
                    "type": "text",
                    "text": "[UpgradeVision] The perception above was captured "
                            "at 1080p resolution.",
                })
            som_followup = self._pending_som_followup
            if som_followup is not None:
                self._pending_som_followup = None
                som_content = self._format_som_followup(som_followup)
                if som_content is not None:
                    followup_parts.extend(som_content)
            if followup_parts:
                self.history.append({"role": "user", "content": followup_parts})
            if self._pending_completion is not None:
                # The model called CompleteTask inside this batch: surface its
                # answer as this round's final message so the tool loop stops and
                # run_task can return it without another LLM round-trip.
                self.history.append(
                    {"role": "assistant", "content": self._pending_completion}
                )
                return self._pending_completion

    def _batch_signature(self, tool_calls: list[Any]) -> tuple[tuple[str, str], ...]:
        """Canonical, order-preserving signature of a tool-call batch.

        Used to detect when the LLM re-emits the exact same batch it already
        ran on the previous round (a common token-wasting loop).
        """
        sig: list[tuple[str, str]] = []
        for call in tool_calls:
            name = call.function.name
            try:
                args: Any = json.loads(call.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = call.function.arguments
            sig.append((name, json.dumps(args, sort_keys=True, ensure_ascii=False)))
        return tuple(sig)

    async def _execute_tool_calls(self, tool_calls: list[Any]) -> list[dict[str, Any]]:
        sig = self._batch_signature(tool_calls)
        if sig and sig == self._last_batch_signature and self._last_batch_all_succeeded:
            logger.info(
                "Skipping identical repeated tool-call batch (%d call(s))", len(tool_calls)
            )
            return [
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": (
                        "[notice] You repeated the exact same tool call(s) that already "
                        "succeeded; the previous result is unchanged. Proceed with the "
                        "next step or choose a different action."
                    ),
                }
                for call in tool_calls
            ]

        results = []
        succeeded: list[bool] = []
        llm_tools = self.llm.tool_names()
        for call in tool_calls:
            if self._check_cancelled():
                results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "[error] Task cancelled by kill switch.",
                })
                succeeded.append(False)
                break

            name = call.function.name
            args = json.loads(call.function.arguments)
            if name in llm_tools:
                # Built-in Formula or local function tool handled by LLM client.
                await self.eventbus.emit(
                    ToolCallRequested(
                        server="local", tool_name=name, arguments=args,
                        task_id=self.task_id,
                    )
                )
                local_level = _LOCAL_TOOL_SECURITY.get(name)
                if local_level is not None:
                    approval = self.security.check(
                        local_level,
                        {"server": "local", "tool": name, "args": args},
                    )
                    if not approval.allowed:
                        self.consecutive_action_failures += 1
                        self._round_tool_failed = True
                        results.append({
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": f"[blocked] {approval.reason}",
                        })
                        succeeded.append(False)
                        await self.eventbus.emit(
                            ToolCallCompleted(
                                server="local",
                                tool_name=name,
                                result=f"[blocked] {approval.reason}",
                                success=False,
                                task_id=self.task_id,
                            )
                        )
                        continue
                outputs = await self.llm.execute_tool_calls([call])
                results.extend(outputs)
                succeeded.extend([True] * len(outputs))
                local_content = "".join(str(o.get("content", "")) for o in outputs)
                await self.eventbus.emit(
                    ToolCallCompleted(
                        server="local",
                        tool_name=name,
                        result=local_content,
                        success=not local_content.startswith("[error]"),
                        task_id=self.task_id,
                    )
                )
                # ViewMedia results carry "[media_ref] kind ms://url" markers;
                # lift them into real media parts injected after this batch.
                for output in outputs:
                    for kind, url in parse_media_refs(str(output.get("content", ""))):
                        key = "video_url" if kind == "video" else "image_url"
                        self._pending_media_parts.append({"type": key, key: {"url": url}})
                if self._is_ui_tool(name, None):
                    self._used_ui_tool = True
                continue

            server, tool_name = self._resolve_mcp_tool(name)
            if not server:
                self.consecutive_action_failures += 1
                results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"[error] Tool {name} not found.",
                })
                succeeded.append(False)
                continue
            if self._is_ui_tool(name, server):
                self._used_ui_tool = True

            if (
                server == "windows"
                and tool_name in _POSITIONAL_WINDOWS_TOOLS
                and not (args.get("loc") or args.get("label") is not None)
            ):
                self.consecutive_action_failures += 1
                self._round_tool_failed = True
                results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": (
                        f"[error] windows/{tool_name} requires a `label` (element id from "
                        f"windows__Snapshot) or `loc` [x, y]. Call windows__Snapshot first, "
                        f"read the target [id], then retry {tool_name} with label=<id>."
                    ),
                })
                succeeded.append(False)
                continue

            await self.eventbus.emit(
                ToolCallRequested(
                    server=server, tool_name=tool_name, arguments=args, task_id=self.task_id
                )
            )
            level = self.security.classify_tool_call(server, tool_name)
            approval = self.security.check(level, {"server": server, "tool": tool_name, "args": args})
            if not approval.allowed:
                content = f"[blocked] {approval.reason}"
                success = False
            else:
                result: ToolResult = await self.mcp.call(server, tool_name, args)
                content = result.content
                success = result.success
                if not success and _STALE_LABEL_RE.search(content):
                    content += (
                        "\n[hint] Labels are invalidated whenever a new "
                        "Snapshot/Screenshot is taken. Call windows__Snapshot "
                        "now to get fresh labels, then retry this action with "
                        "the new label."
                    )
            await self.eventbus.emit(
                ToolCallCompleted(
                    server=server,
                    tool_name=tool_name,
                    result=content,
                    success=success,
                    task_id=self.task_id,
                )
            )
            self.memory.audit(
                level=level,
                actor=f"mcp:{server}",
                action=f"{tool_name}({json.dumps(self._redact_args(args), ensure_ascii=False)})",
                result=content[:500],
            )
            self.last_action_summary = f"{server}/{tool_name}: {content[:200]}"
            self.action_traces.append(self.last_action_summary)
            if not success:
                self.consecutive_action_failures += 1
                self._round_tool_failed = True
            else:
                self.consecutive_action_failures = 0
            results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": content,
            })
            succeeded.append(success)
        self._last_batch_signature = sig
        self._last_batch_all_succeeded = bool(succeeded) and all(succeeded)
        return results

    @staticmethod
    def _redact_args(args: dict[str, Any]) -> dict[str, Any]:
        """Mask sensitive argument values before they reach the audit log."""
        if not isinstance(args, dict):
            return args
        return {
            k: ("***" if k.lower() in _SENSITIVE_ARG_KEYS else v)
            for k, v in args.items()
        }

    def _resolve_mcp_tool(self, name: str) -> tuple[str | None, str | None]:
        for tool in self.mcp.all_tools():
            full_name = f"{tool['server']}__{tool['name']}"
            if full_name == name:
                return tool["server"], tool["name"]
        return None, None

    def _last_action_was_query(self) -> bool:
        """Return True if the most recent successful tool call was read-only."""
        summary = (self.last_action_summary or "").lower()
        query_indicators = ["read", "list", "get", "find", "search", "snapshot", "screenshot"]
        return any(ind in summary for ind in query_indicators)

    @staticmethod
    def _is_ui_tool(name: str, server: str | None) -> bool:
        """Return True if a tool call interacts with the desktop/browser screen.

        MCP servers ``windows`` and ``playwright`` operate on the screen, as does
        the local ``desktop_interact`` tool (which clicks via Windows-MCP).
        Everything else (filesystem MCP, Kimi Formula tools, code runners) does
        not change the UI.
        """
        if server in {"windows", "playwright"}:
            return True
        if name in {"desktop_interact", "DesktopInteract"}:
            return True
        return False

    async def _verify(
        self,
        previous_perception: Any | None,
        current_perception: Any | None,
    ) -> bool:
        """Verify that the last action made progress.

        Combines three signals:
        1. LLM judgment (YES/NO) on whether the action was sufficient.
        2. Whether the UI state changed between before and after.
        3. Whether the last action was a read/query operation.

        If the LLM says YES and either the UI changed or the action was a query,
        we trust the result. If the LLM says NO or the UI is unchanged after a
        mutating action, verification fails.
        """
        if self._round_tool_failed:
            # A tool in this round failed (e.g. Type without label). Do not let
            # a hallucinated YES plus an unrelated UI change pass as COMPLETED;
            # force a reflect/replan instead.
            return False

        self.history.append({
            "role": "user",
            "content": (
                "Did the last action successfully complete the user's request, or make "
                "clear, sufficient progress toward it? Reply with a single word: YES or NO."
            ),
        })
        completion = await self._llm_chat_with_breaker(self.history)
        text = (completion.choices[0].message.content or "").strip().upper()
        self.history.append({"role": "assistant", "content": text})
        llm_says_yes = text.startswith("YES")

        if not llm_says_yes:
            return False

        # Tasks that never touched the screen (pure compute / API / filesystem)
        # cannot be judged by UI changes; trust the model's YES.
        if not self._used_ui_tool:
            return True

        # For mutating actions, require evidence of UI change.
        if previous_perception is not None and current_perception is not None:
            changed = self.perception.has_changed(previous_perception, current_perception)
            if changed:
                return True
            if not self._last_action_was_query():
                return False
        return True

    def _schedule_skill_learning(self) -> None:
        """Fire-and-forget skill learning so it never blocks the final answer.

        The task is tracked in ``_background_tasks`` (so it isn't GC'd and can
        be drained on shutdown); failures are handled inside ``_learn_skill``.
        """
        if self.skill_learner is None:
            return
        task = asyncio.create_task(self._learn_skill())
        self._background_tasks.add(task)
        task.add_done_callback(self._background_tasks.discard)

    async def _learn_skill(self) -> None:
        """Record a reusable skill after a successful task.

        Failures are logged but never surface to the user; skill learning is
        best-effort.
        """
        if self.skill_learner is None:
            return
        try:
            await self.skill_learner.learn(
                self.current_instruction, list(self.action_traces)
            )
            logger.info("Skill learned for task: %s", self.current_instruction)
        except Exception as exc:
            logger.warning("Failed to learn skill: %s", exc)

    async def _final_answer(self) -> str:
        self.history.append({
            "role": "user",
            "content": (
                "Provide the final answer to the user. Summarize what you found. "
                "Do not call any tools."
            ),
        })
        for attempt in range(3):
            completion = await self._llm_chat_with_breaker(self.history, tools=None)
            message = completion.choices[0].message
            content = message.content or ""
            tool_calls = getattr(message, "tool_calls", None) or []
            if not tool_calls:
                self.history.append({"role": "assistant", "content": content})
                await self.state.transition("COMPLETED", task_id=self.task_id)
                return content
            # The model tried to call tools despite being asked not to.
            self.history.append({
                "role": "assistant",
                "content": content or "[attempted tool call]",
            })
            self.history.append({
                "role": "user",
                "content": (
                    "You are asked to provide the final answer only. "
                    "Do not call tools."
                ),
            })
        await self.state.transition("ERROR", task_id=self.task_id)
        return "Failed to produce a final answer without tool calls."

    async def _reflect(self) -> str:
        self.history.append({
            "role": "user",
            "content": (
                "The last action did not succeed or the UI state is unclear. "
                "Reflect on what went wrong and propose the next step."
            ),
        })
        completion = await self._llm_chat_with_breaker(self.history)
        content = completion.choices[0].message.content or ""
        self.history.append({"role": "assistant", "content": content})
        return content

    async def _maybe_extend_loop_limit(self, current_limit: int) -> int:
        """Loop-limit checkpoint: reflect on whether the approach is sound.

        Called when the current loop budget is exhausted without completing
        the task. A YES extends the budget by ``_LOOP_LIMIT_INCREMENT`` (hard
        cap ``_MAX_LOOP_LIMIT``); a NO — or a failed reflection — returns the
        current limit unchanged so the task stops as stuck. At the hard cap no
        LLM call is made.
        """
        if current_limit >= _MAX_LOOP_LIMIT:
            return current_limit
        self.history.append({
            "role": "user",
            "content": (
                f"You have used {current_limit} perception-action loops without "
                "completing the task. Review the trajectory so far: is the "
                "current approach fundamentally sound and making progress, "
                "simply needing more steps? Reply YES to continue with more "
                "loops, or NO if the approach is wrong and the task should "
                "stop. Reply with a single word: YES or NO."
            ),
        })
        try:
            completion = await self._llm_chat_with_breaker(self.history)
        except APIBreakerTripped:
            raise
        except Exception as exc:
            logger.warning("Loop-extension reflection failed: %s", exc)
            self.history.append(
                {"role": "assistant", "content": "[reflection failed]"}
            )
            return current_limit
        answer = (completion.choices[0].message.content or "").strip().upper()
        self.history.append({"role": "assistant", "content": answer})
        if not answer.startswith("YES"):
            return current_limit
        new_limit = min(current_limit + _LOOP_LIMIT_INCREMENT, _MAX_LOOP_LIMIT)
        # Stash the confirmation for the next perception message instead of
        # appending it here: the very next thing run_task does is append the
        # perception user message, and back-to-back user turns are rejected.
        self._pending_loop_notice = (
            f"Approach confirmed sound. You have up to "
            f"{new_limit - current_limit} more loops. Continue from where "
            "you left off."
        )
        return new_limit
