"""ReAct loop orchestrator: Perceive → Reflect → Think → Act → Verify."""

from __future__ import annotations

import asyncio
import base64
import json
import logging
from collections import deque
from typing import Any

import httpx
import openai

from agent.config import Config
from agent.kill_switch import KillSwitch
from agent.kimi_memory import KimiMemoryClient
from agent.llm_client import LLMClient
from agent.memory import MemoryStore
from agent.perception import PerceptionModule
from agent.reflection import ReflectionEngine
from agent.security import SecurityGuard
from agent.skills import SkillLearner
from agent.state_machine import AgentStateMachine
from agent.tools import DESKTOP_INTERACT_SCHEMA, register_all
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
        self._human_confirm_callback: Any | None = None
        self.eventbus.subscribe("KillSwitchTriggered", self._on_kill_switch)

    def set_human_confirmation_callback(self, callback: Any) -> None:
        self._human_confirm_callback = callback

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
        await self.llm.initialize()
        await self.mcp.connect_all()
        register_all(self.llm, self.mcp)
        self._register_desktop_interact()
        if self.config.ui_detector.enabled:
            from ui_detector import UIDetector

            self.ui_detector = UIDetector(self.config.ui_detector)
            self.ui_detector.load()
            self.perception.ui_detector = self.ui_detector
        self._load_state()
        self.kill_switch.start()

    async def shutdown(self) -> None:
        self._save_state()
        self.kill_switch.stop()
        await self.mcp.disconnect_all()
        await self.llm.close()
        self.perception.shutdown()
        if self.ui_detector is not None:
            self.ui_detector.shutdown()

    def _save_state(self) -> None:
        payload = {
            "state": self.state.current_state,
            "task_id": self.task_id,
            "current_instruction": self.current_instruction,
            "consecutive_action_failures": self.consecutive_action_failures,
            "consecutive_api_failures": self.consecutive_api_failures,
            "history": self.history,
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
            self.history = payload.get("history", [])
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

    def _register_desktop_interact(self) -> None:
        """Register the DesktopInteract local function tool with the LLM."""
        self.llm.register_local_function(
            "DesktopInteract",
            self._desktop_interact_impl,
            schema=DESKTOP_INTERACT_SCHEMA,
            description=(
                "Interact with a UI element identified by a SoM (Set-of-Mark) label number. "
                "The screenshot shows numbered red circles on detected elements. "
                "Use the label number to click, double-click, right-click, type text, or scroll. "
                "For 'type' action, provide the 'text' parameter."
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

    async def run_task(self, user_input: str, task_id: str | None = None) -> str:
        self.task_id = task_id or "task-0"
        self.current_instruction = user_input
        self._cancel_event.clear()
        self.consecutive_action_failures = 0
        self.consecutive_api_failures = 0
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
            "- For unmarked elements, use the raw MCP tools with explicit coordinates or refs."
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
        self.action_traces = []

        max_loops = 10
        for loop in range(max_loops):
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
            perception = await self.perception.perceive(instruction=self.current_instruction)
            self._last_perception = perception
            self.history.append({
                "role": "user",
                "content": self._format_perception(perception),
            })

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

            if self._is_same_ui_loop(perception.ui_hash):
                loops = len(self._recent_hashes)
                reason = f"UI state unchanged for {loops} loops"
                await self.reflection.record(
                    task_summary=user_input,
                    failure_reason=reason,
                    fix_action="Change the approach or ask for human guidance.",
                )
                await self.state.transition("STUCK", task_id=self.task_id)
                return f"{reason}; agent is stuck."

            previous_perception = self._last_perception
            current_perception = perception
            self._last_perception = perception

            try:
                response = await self._think_and_act()
                if self.state.current_state == "COMPLETED":
                    return response

                if self._check_cancelled():
                    await self.state.transition("IDLE", task_id=self.task_id)
                    return "Task cancelled by kill switch."

                # Capture the post-action perception for state-based verification.
                post_action_perception = await self.perception.perceive(instruction=self.current_instruction)
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
                        await self._learn_skill()
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

        if self.state.current_state not in {"COMPLETED", "ERROR"}:
            await self.state.transition("STUCK", task_id=self.task_id)
            await self.reflection.record(
                task_summary=user_input,
                failure_reason="Exceeded maximum loop count",
                fix_action="Break task into smaller steps.",
            )
            return "Agent reached the loop limit without completing the task."
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
                return content

            self.history.append({
                "role": "assistant",
                "content": content,
                "tool_calls": [c.model_dump() for c in tool_calls],
            })

            tool_results = await self._execute_tool_calls(tool_calls)
            self.history.extend(tool_results)

    async def _execute_tool_calls(self, tool_calls: list[Any]) -> list[dict[str, Any]]:
        results = []
        llm_tools = self.llm.tool_names()
        for call in tool_calls:
            if self._check_cancelled():
                results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": "[error] Task cancelled by kill switch.",
                })
                break

            name = call.function.name
            args = json.loads(call.function.arguments)
            if name in llm_tools:
                # Built-in Formula tool handled by LLM client.
                outputs = await self.llm.execute_tool_calls([call])
                results.extend(outputs)
                continue

            server, tool_name = self._resolve_mcp_tool(name)
            if not server:
                self.consecutive_action_failures += 1
                results.append({
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": f"[error] Tool {name} not found.",
                })
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
                action=f"{tool_name}({json.dumps(args, ensure_ascii=False)})",
                result=content[:500],
            )
            self.last_action_summary = f"{server}/{tool_name}: {content[:200]}"
            self.action_traces.append(self.last_action_summary)
            if not success:
                self.consecutive_action_failures += 1
            else:
                self.consecutive_action_failures = 0
            results.append({
                "role": "tool",
                "tool_call_id": call.id,
                "content": content,
            })
        return results

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
        self.history.append({
            "role": "user",
            "content": (
                "Did the last action produce enough information to answer the user's request? "
                "Reply with a single word: YES or NO."
            ),
        })
        completion = await self._llm_chat_with_breaker(self.history)
        text = (completion.choices[0].message.content or "").strip().upper()
        self.history.append({"role": "assistant", "content": text})
        llm_says_yes = text.startswith("YES")

        if not llm_says_yes:
            return False

        # For mutating actions, require evidence of UI change.
        if previous_perception is not None and current_perception is not None:
            changed = self.perception.has_changed(previous_perception, current_perception)
            if changed:
                return True
            if not self._last_action_was_query():
                return False
        return True

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
