"""Event definitions used across Caelum-Agent."""

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class AgentStateChanged:
    old_state: str
    new_state: str
    task_id: str | None = None


@dataclass(frozen=True)
class UserInputReceived:
    text: str
    task_id: str | None = None


@dataclass(frozen=True)
class ToolCallRequested:
    server: str
    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    task_id: str | None = None


@dataclass(frozen=True)
class ToolCallCompleted:
    server: str
    tool_name: str
    result: Any
    success: bool = True
    task_id: str | None = None


@dataclass(frozen=True)
class LLMResponseReceived:
    content: str | None
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    task_id: str | None = None


@dataclass(frozen=True)
class KillSwitchTriggered:
    reason: str
    task_id: str | None = None
