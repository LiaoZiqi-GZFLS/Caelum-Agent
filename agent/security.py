"""Security policy enforcement before risky actions."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

from agent.config import SecurityConfig

ConfirmationCallback = Callable[[str, dict[str, Any]], bool]


@dataclass(frozen=True)
class Approval:
    allowed: bool
    reason: str
    level: str


class SecurityGuard:
    def __init__(
        self,
        config: SecurityConfig,
        confirm_callback: ConfirmationCallback | None = None,
    ) -> None:
        self.config = config
        self.confirm_callback = confirm_callback

    def check(self, action_level: str, action: dict[str, Any]) -> Approval:
        """Return whether an action may proceed without human confirmation."""
        if action_level in self.config.auto_execute_levels:
            return Approval(allowed=True, reason="auto-execute", level=action_level)
        if action_level in self.config.confirm_levels:
            return self._request_confirmation(action_level, action)
        if self.config.destructive_requires_approval and action_level == "destructive":
            return self._request_confirmation(action_level, action)
        return Approval(allowed=True, reason="default allow", level=action_level)

    def _request_confirmation(self, action_level: str, action: dict[str, Any]) -> Approval:
        summary = self._summarize(action)
        if self.confirm_callback is None:
            return Approval(
                allowed=False,
                reason=f"{action_level}: {summary} (no confirmation handler configured)",
                level=action_level,
            )
        if action_level == "destructive" and self.config.destructive_requires_typed_confirmation:
            if not self._typed_confirmation(summary):
                return Approval(allowed=False, reason="human-denied", level=action_level)
            return Approval(allowed=True, reason="human-confirmed", level=action_level)
        if self.confirm_callback(summary, action):
            return Approval(allowed=True, reason="human-confirmed", level=action_level)
        return Approval(allowed=False, reason="human-denied", level=action_level)

    def _typed_confirmation(self, summary: str) -> bool:
        """Require the user to re-type the action summary to confirm a destructive action."""
        if self.confirm_callback is None:
            return False
        # First show the prompt via the callback; if it returns False, abort early.
        if not self.confirm_callback(summary, {"typed_confirmation": True}):
            return False
        try:
            answer = input("Retype the action summary to confirm: ").strip()
        except EOFError:
            return False
        return answer == summary

    @staticmethod
    def _summarize(action: dict[str, Any]) -> str:
        server = action.get("server", "unknown")
        tool = action.get("tool", "unknown")
        args = action.get("args", {})
        return f"{server}/{tool}({', '.join(f'{k}={v!r}' for k, v in args.items())})"

    def classify_tool_call(self, server: str, tool_name: str) -> str:
        """Map a tool call to a security level."""
        destructive = {"delete", "remove", "format", "registry", "powershell"}
        risky = {
            "write",
            "edit",
            "type",
            "click",
            "move",
            "shortcut",
            "browser_click",
            "browser_type",
            "browser_fill_form",
            "browser_evaluate",
            "browser_route",
            "windows/app",
            "multiedit",
            "windows/filesystem",
        }
        lowered = f"{server}/{tool_name}".lower()
        if any(d in lowered for d in destructive):
            return "destructive"
        if any(r in lowered for r in risky):
            return "write_risky"
        return "read"
