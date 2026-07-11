"""Shared fake implementations for test suites."""

from __future__ import annotations

import copy
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from agent.kill_switch import KillSwitch
from agent.perception import Perception, PerceptionModule
from agent.reflection import ReflectionEngine
from eventbus import EventBus
from eventbus.events import KillSwitchTriggered
from mcp_client import ToolResult


class FakeLLM:
    """Scripted fake LLM usable as both chat client and tool executor.

    Three input modes (queues) that compose cleanly:

    1. ``chat_responses`` — ChatCompletion-like objects (or Exceptions)
       returned by ``chat()``. Indexed sequentially.
    2. ``tool_responses`` — lists of tool-result dicts returned by
       ``execute_tool_calls()``. Indexed sequentially.
    3. Combined — queues are independent. The orchestrator uses both.

    Each queue can be left empty; sensible defaults apply.
    """

    def __init__(
        self,
        chat_responses: list[Any] | None = None,
        tool_responses: list[list[dict[str, Any]]] | None = None,
        tool_names: list[str] | None = None,
        default_chat: Any | None = None,
    ) -> None:
        self._chat_queue = list(chat_responses or [])
        self._tool_queue = list(tool_responses or [])
        self._default_chat = default_chat
        self._chat_index = 0
        self._tool_index = 0
        # Public recording fields.
        self.calls: list[list[dict[str, Any]]] = []
        self.last_tools: list[Any] = []
        self.chat_kwargs: list[dict[str, Any]] = []
        # tools holds both constructor-provided names and registered names.
        self.tools: list[str] = list(tool_names or [])

    def register_function_tools(self, tools: list[dict[str, Any]]) -> None:
        for t in tools:
            self.tools.append(t["function"]["name"])

    def register_local_function(self, name: str, fn: Any, **kwargs: Any) -> None:
        self.tools.append(name)

    def tool_names(self) -> list[str]:
        return self.tools

    async def initialize(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def chat(
        self, messages: list[dict[str, Any]], tools: Any = None, **kwargs: Any
    ) -> Any:
        self.calls.append(messages)
        self.last_tools.append(tools)
        self.chat_kwargs.append(kwargs)
        response: Any
        if self._chat_index < len(self._chat_queue):
            response = self._chat_queue[self._chat_index]
        elif self._default_chat is not None:
            response = self._default_chat
        else:
            raise RuntimeError(
                f"FakeLLM ran out of chat responses after {self._chat_index} calls"
            )
        self._chat_index += 1
        if isinstance(response, Exception):
            raise response
        return response

    async def execute_tool_calls(
        self, calls: list[Any]
    ) -> list[dict[str, Any]]:
        self.calls.append(calls)
        if self._tool_index < len(self._tool_queue):
            result = self._tool_queue[self._tool_index]
            self._tool_index += 1
            return result
        return [
            {"role": "tool", "tool_call_id": call.id, "content": "{}"}
            for call in calls
        ]


class FakeMCP:
    """In-memory MCP multiplexer with call recording and result stubbing."""

    def __init__(self, tools: list[dict[str, Any]] | None = None) -> None:
        self._tools = tools or []
        self.calls: list[tuple[str, str, dict[str, Any]]] = []
        self._results: dict[tuple[str, str], ToolResult] = {}

    def set_result(self, server: str, tool: str, result: ToolResult) -> None:
        self._results[(server, tool)] = result

    async def connect_all(self) -> None:
        pass

    async def disconnect_all(self) -> None:
        pass

    async def call(
        self, server: str, tool_name: str, arguments: dict[str, Any]
    ) -> ToolResult:
        self.calls.append((server, tool_name, arguments))
        return self._results.get(
            (server, tool_name),
            ToolResult(success=True, content=f"{server}/{tool_name} ok"),
        )

    def all_tools(self) -> list[dict[str, Any]]:
        return self._tools


class FakeKillSwitch(KillSwitch):
    """Kill switch that never listens to pynput; trigger manually."""

    def __init__(self, eventbus: EventBus) -> None:
        self.eventbus = eventbus
        self.started = False
        self.stopped = False

    def start(self) -> None:
        self.started = True

    def stop(self) -> None:
        self.stopped = True

    async def trigger(self) -> None:
        await self.eventbus.emit(KillSwitchTriggered(reason="test"))


class FakePerception(PerceptionModule):
    """Perception module that replays pre-baked Perception objects."""

    def __init__(self, perceptions: list[Perception] | None = None) -> None:
        self.perceptions = list(perceptions or [])
        self._index = 0
        self.calls: list[str] = []

    async def perceive(self, instruction: str = "", with_vision: bool = False) -> Perception:
        self.calls.append(instruction)
        if self._index >= len(self.perceptions):
            base = (
                self.perceptions[-1]
                if self.perceptions
                else _blank_perception()
            )
        else:
            base = self.perceptions[self._index]
        self._index += 1
        perception = copy.copy(base)
        if not perception.ui_hash:
            perception.ui_hash = f"fake-{self._index - 1}"
        return perception

    async def perceive_with_vision(self, instruction: str = "") -> Perception:
        return await self.perceive(instruction=instruction, with_vision=True)


class FakeReflection(ReflectionEngine):
    """Reflection engine that records entries in a list."""

    def __init__(self) -> None:
        self.recorded: list[dict[str, Any]] = []

    def build_context(self, user_input: str) -> str:
        return ""

    async def record(
        self, task_summary: str, failure_reason: str, fix_action: str
    ) -> None:
        self.recorded.append({
            "task_summary": task_summary,
            "failure_reason": failure_reason,
            "fix_action": fix_action,
        })


class FakeSkillLearner:
    """Skill learner that records (task, trajectory) calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, list[str]]] = []

    async def learn(
        self, task: str, trajectory: list[str]
    ) -> dict[str, Any]:
        self.calls.append((task, list(trajectory)))
        return {
            "name": "learned",
            "version": "v0.1.0",
            "path": "/tmp/learned.md",
            "merged": False,
        }


class TriggeringLLM(FakeLLM):
    """Fake LLM that fires the kill switch on the first chat() call."""

    def __init__(
        self,
        responses: list[Any],
        killswitch: FakeKillSwitch,
    ) -> None:
        super().__init__(chat_responses=responses)
        self._killswitch = killswitch

    async def chat(
        self, messages: list[dict[str, Any]], tools: Any = None
    ) -> Any:
        if self._chat_index == 0:
            await self._killswitch.trigger()
        return await super().chat(messages, tools)


# -- Helpers (formerly in test_orchestrator.py) --------------------------------

def _blank_perception() -> Perception:
    """Return a minimal Perception for tests that just need one."""
    return Perception(
        screenshot_path=Path("/tmp/blank.jpg"),
        description="Blank screen",
        ocr_text="",
        ui_tree={},
        som_annotations=[],
    )


def _same_hash_perception(hash_value: str = "same") -> Perception:
    """Return a Perception with a fixed ui_hash for loop-detection tests."""
    return Perception(
        screenshot_path=Path("/tmp/blank.jpg"),
        description="Same screen",
        ocr_text="same",
        ui_tree={"same": True},
        som_annotations=[],
        ui_hash=hash_value,
    )


def _message(content: str = "", tool_calls: list[Any] | None = None) -> Any:
    """Build a ChatCompletion-like object from content + optional tool_calls."""
    return SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=content,
                    tool_calls=tool_calls or [],
                )
            )
        ]
    )


class _FakeToolCall:
    """A tool-call object that mimics OpenAI's ToolCall with model_dump()."""

    def __init__(
        self, name: str, args: dict[str, Any], call_id: str = "call_1"
    ) -> None:
        self.id = call_id
        import json as _json

        self.function = SimpleNamespace(
            name=name, arguments=_json.dumps(args)
        )

    def model_dump(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "function": {
                "name": self.function.name,
                "arguments": self.function.arguments,
            },
        }


def _tool_call(
    name: str, args: dict[str, Any], call_id: str = "call_1"
) -> Any:
    """Shorthand to create a FakeToolCall."""
    return _FakeToolCall(name, args, call_id)
